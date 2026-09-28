# realtime_four_state_best_original.py
from __future__ import annotations

import argparse
import csv
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, List

import joblib
import numpy as np
from connector import WiFiDataProcessor

import socket

EPS = 1e-6
N_RAW_SC = 64

# 4-State Constants
EMPTY = 0
LYING = 1
STANDING = 2
MOVING = 3
DISPLAY = {EMPTY: "Empty", LYING: "Lying", STANDING: "Standing", MOVING: "Moving"}
ACT_NAMES = ["LYING", "STANDING", "MOVING"] # Activity model index (0, 1, 2)

def packet_to_amp_rssi(pkt, valid_sc, n_raw_sc) -> Tuple[np.ndarray, float]:
    csi = np.asarray(pkt["csi"], dtype=np.float32)
    if csi.ndim != 2 or csi.shape[-1] != 2:
        raise ValueError(f"CSI shape error: {csi.shape}")

    n_sc = csi.shape[0]
    if n_sc < n_raw_sc:
        csi = np.concatenate(
            [csi, np.zeros((n_raw_sc-n_sc, 2), dtype=np.float32)], axis=0
        )
    elif n_sc > n_raw_sc:
        csi = csi[:n_raw_sc]

    I = np.fft.fftshift(csi[:,0], axes=0)[valid_sc]
    Q = np.fft.fftshift(csi[:,1], axes=0)[valid_sc]
    amp = np.sqrt(I*I + Q*Q).astype(np.float32)
    return amp, float(pkt.get("rssi", 0.0))

def collect_empty_baseline(processor, valid_sc, n_raw_sc, duration_sec) -> Dict[str, object]:
    print("\n" + "="*90)
    print("EMPTY ROOM CALIBRATION")
    print("="*90)
    input("방을 완전히 비운 뒤 Enter를 누르세요...")
    print("5초 뒤 calibration 시작...")
    time.sleep(5)

    processor.sync_start(number_to_ignore=30)
    amps, rssis = [], []
    t0 = time.time()

    while time.time()-t0 < duration_sec:
        try:
            pkt = processor.get_wifi_data_dict(timeout=1.0)
            amp, rssi = packet_to_amp_rssi(pkt, valid_sc, n_raw_sc)
            amps.append(amp)
            rssis.append(rssi)
        except Exception:
            pass

    if len(amps) < 100:
        raise RuntimeError(f"Too few calibration packets: {len(amps)}")

    amp_arr = np.vstack(amps).astype(np.float32)
    rssi_arr = np.asarray(rssis, dtype=np.float32)

    baseline = {
        "amp_median": np.median(amp_arr, axis=0).astype(np.float32),
        "rssi_median": float(np.median(rssi_arr)),
    }

    print("\nCalibration complete")
    print(f"packets={len(amp_arr)}")
    print(f"Amplitude mean/std={np.mean(amp_arr):.3f}/{np.std(amp_arr):.3f}")
    print(f"RSSI median={baseline['rssi_median']:.2f}")
    print("이후 같은 CSI stream을 그대로 사용합니다.")
    return baseline

def temporal_zscore(x):
    mu = np.mean(x, axis=0, keepdims=True)
    sd = np.std(x, axis=0, keepdims=True)
    z = (x-mu)/(sd+EPS)
    return np.clip(
        np.nan_to_num(z, nan=0.0, posinf=8.0, neginf=-8.0), -8, 8
    ).astype(np.float32)

def relative_delta(x, baseline):
    rel = (x-baseline[None,:])/(np.abs(baseline[None,:])+EPS)
    return np.clip(
        np.nan_to_num(rel, nan=0.0, posinf=5.0, neginf=-5.0), -5, 5
    ).astype(np.float32)

def make_presence_input(amp_window, baseline_amp):
    x = np.concatenate(
        [temporal_zscore(amp_window), relative_delta(amp_window, baseline_amp)],
        axis=1
    )
    return x[None,...].astype(np.float32)

def sc_stats(x):
    d = np.diff(x, axis=0)
    ad = np.abs(d)
    xmax, xmin = np.max(x, axis=0), np.min(x, axis=0)
    return np.concatenate([
        np.mean(x, axis=0), np.std(x, axis=0), xmax, xmin,
        np.median(x, axis=0), xmax-xmin,
        np.std(d, axis=0), np.mean(ad, axis=0), np.percentile(ad,95,axis=0)
    ]).astype(np.float32)

def make_activity_input(amp_window, rssi_window, baseline_rssi):
    dr = rssi_window.astype(np.float32) - float(baseline_rssi)
    rssi_feat = np.array([
        np.mean(dr), np.std(dr), np.min(dr), np.max(dr), np.max(dr)-np.min(dr)
    ], dtype=np.float32)
    feat = np.concatenate([sc_stats(amp_window), rssi_feat])
    return feat.reshape(1,-1).astype(np.float32)

class ActivityStateMachineMulti:
    """
    다중 클래스(Lying, Standing, Moving)를 위한 State Machine
    특정 클래스의 확률이 threshold 이상으로 count 회 연속 발생하면 해당 상태로 확정.
    """
    def __init__(self, thresholds: Dict[int, float], counts: Dict[int, int]):
        self.thresholds = thresholds
        self.counts = counts
        self.state: Optional[int] = None
        self.streaks = {0: 0, 1: 0, 2: 0} # 0:Lying, 1:Standing, 2:Moving

    def reset(self):
        self.state = None
        self.streaks = {0: 0, 1: 0, 2: 0}

    def update(self, probs: np.ndarray) -> Tuple[int, str]:
        transition = ""
        pred_class = int(np.argmax(probs))
        max_prob = probs[pred_class]

        # 초기 상태 설정
        if self.state is None:
            self.state = pred_class
            transition = f"INIT->{ACT_NAMES[self.state]}"
            return self.state, transition

        # 상태 전환 조건 충족 확인
        if pred_class != self.state and max_prob >= self.thresholds[pred_class]:
            self.streaks[pred_class] += 1
            # 다른 streak 리셋
            for k in self.streaks:
                if k != pred_class: self.streaks[k] = 0

            # streak이 count 임계치를 넘으면 상태 전환
            if self.streaks[pred_class] >= self.counts[pred_class]:
                transition = f"{ACT_NAMES[self.state]}->{ACT_NAMES[pred_class]}"
                self.state = pred_class
                self.streaks[pred_class] = 0
        else:
            # 유지 혹은 불확실한 상태면 모든 streak 초기화 (연속성 단절)
            self.streaks = {0: 0, 1: 0, 2: 0}

        return self.state, transition


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=921600)
    # 디렉토리와 아웃풋 경로명 4-state로 변경
    ap.add_argument("--model_dir", default=r".\models\four_state_best_original")
    ap.add_argument("--baseline_sec", type=float, default=None)
    
    # 3개 클래스별 Threshold/Count 인자
    ap.add_argument("--prob_th_lying", type=float, default=0.70)
    ap.add_argument("--count_th_lying", type=int, default=3)
    ap.add_argument("--prob_th_standing", type=float, default=0.70)
    ap.add_argument("--count_th_standing", type=int, default=3)
    ap.add_argument("--prob_th_moving", type=float, default=0.80)
    ap.add_argument("--count_th_moving", type=int, default=3)
    
    ap.add_argument("--duration_sec", type=float, default=0.0)
    ap.add_argument("--output", default=r".\results\four_state_realtime.csv")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    try:
        import tensorflow as tf
    except Exception as e:
        raise ImportError("TensorFlow required: pip install tensorflow") from e

    model_dir = Path(args.model_dir)
    presence_model = tf.keras.models.load_model(model_dir/"presence_cnn.keras")
    pm = joblib.load(model_dir/"presence_meta.joblib")
    ab = joblib.load(model_dir/"activity_xgb.pkl")
    activity_model = ab["model"]
    ai = ab["info"]

    p_window = int(pm["window"])
    a_window = int(ai["window"])
    slide = int(pm["slide"])
    fs = float(pm["fs"])
    p_threshold = float(pm["occupied_threshold"])
    valid_sc = np.asarray(pm["valid_subcarriers"], dtype=np.int32)
    n_raw_sc = int(pm.get("n_raw_subcarriers", N_RAW_SC))
    feature_count = int(ai["feature_count"])

    if not np.array_equal(valid_sc, np.asarray(ai["valid_subcarriers"], dtype=np.int32)):
        raise RuntimeError("Presence / Activity subcarrier mismatch")

    baseline_sec = (
        float(args.baseline_sec)
        if args.baseline_sec is not None
        else float(pm.get("baseline_sec",30.0))
    )

    print("\n" + "="*105)
    print("REALTIME: EMPTY / LYING / STANDING / MOVING")
    print("="*105)
    print(f"Presence CNN : {p_window/fs:.1f}s | threshold={p_threshold:.4f}")
    print(f"Activity XGB : {a_window/fs:.1f}s (Multi-Class: Lying, Standing, Moving)")
    print(f"  -> To Lying   : Prob >= {args.prob_th_lying:.2f} x {args.count_th_lying} times")
    print(f"  -> To Standing: Prob >= {args.prob_th_standing:.2f} x {args.count_th_standing} times")
    print(f"  -> To Moving  : Prob >= {args.prob_th_moving:.2f} x {args.count_th_moving} times")

    processor = WiFiDataProcessor(port=args.port, baudrate=args.baud, debug=False)
    csv_file = None

    try:
        baseline = collect_empty_baseline(
            processor, valid_sc, n_raw_sc, baseline_sec
        )

        amp_buf = deque(maxlen=max(p_window, a_window))
        rssi_buf = deque(maxlen=max(p_window, a_window))
        
        # State Machine 인스턴스화
        sm = ActivityStateMachineMulti(
            thresholds={0: args.prob_th_lying, 1: args.prob_th_standing, 2: args.prob_th_moving},
            counts={0: args.count_th_lying, 1: args.count_th_standing, 2: args.count_th_moving}
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        last_sent_state = None

        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        csv_file = open(out, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(csv_file)
        writer.writerow([
            "timestamp","elapsed_sec","packet_count",
            "p_occupied","presence_threshold","presence_state",
            "p_lying", "p_standing", "p_moving", "activity_state",
            "activity_transition","final_prediction","final_prediction_name"
        ])

        packet_count = 0
        last_infer_packet = 0
        t0 = time.time()

        print("\n10초 buffer 이후 약 1초마다 결과가 나옵니다.")
        print("[time]     Pocc  Presence  | Ply   Pst   Pmv   Activity | FINAL")

        while True:
            elapsed = time.time()-t0
            if args.duration_sec > 0 and elapsed >= args.duration_sec:
                break

            try:
                pkt = processor.get_wifi_data_dict(timeout=1.0)
                amp, rssi = packet_to_amp_rssi(pkt, valid_sc, n_raw_sc)
            except Exception:
                continue

            packet_count += 1
            amp_buf.append(amp)
            rssi_buf.append(rssi)

            if len(amp_buf) < p_window:
                continue
            if packet_count-last_infer_packet < slide:
                continue

            last_infer_packet = packet_count
            amp_all = np.vstack(amp_buf).astype(np.float32)
            rssi_all = np.asarray(rssi_buf, dtype=np.float32)

            Xp = make_presence_input(
                amp_all[-p_window:], baseline["amp_median"]
            )
            p_occ = float(presence_model.predict(Xp, verbose=0)[0,0])
            occupied = p_occ >= p_threshold

            p_lying, p_standing, p_moving = 0.0, 0.0, 0.0
            activity_state = -1
            transition = ""

            if not occupied:
                sm.reset()
                final_state = EMPTY
            else:
                Xa = make_activity_input(
                    amp_all[-a_window:],
                    rssi_all[-a_window:],
                    baseline["rssi_median"],
                )
                if Xa.shape[1] != feature_count:
                    raise RuntimeError(
                        f"Activity feature mismatch: {Xa.shape[1]} != {feature_count}"
                    )
                # 다중 클래스 확률 추출 (Lying, Standing, Moving)
                probs = activity_model.predict_proba(Xa)[0]
                p_lying, p_standing, p_moving = float(probs[0]), float(probs[1]), float(probs[2])
                
                # 상태 머신 업데이트
                activity_state, transition = sm.update(probs)
                
                # 모델 인덱스(0,1,2)를 전체 상태(LYING=1, STANDING=2, MOVING=3)로 매핑
                final_state = activity_state + 1

            if final_state != last_sent_state:
                sock.sendto(DISPLAY[final_state].encode(), ("127.0.0.1", 5005))
                last_sent_state = final_state

            now = datetime.now()
            writer.writerow([
                now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                f"{elapsed:.4f}", packet_count,
                f"{p_occ:.6f}", f"{p_threshold:.6f}",
                "Occupied" if occupied else "Empty",
                f"{p_lying:.6f}", f"{p_standing:.6f}", f"{p_moving:.6f}",
                activity_state if occupied else -1, transition,
                final_state, DISPLAY[final_state],
            ])
            csv_file.flush()

            a_text = ACT_NAMES[activity_state].capitalize() if occupied else "-"
            
            p_ly_str = f"{p_lying:>4.2f}" if occupied else " -  "
            p_st_str = f"{p_standing:>4.2f}" if occupied else " -  "
            p_mv_str = f"{p_moving:>4.2f}" if occupied else " -  "

            print(
                f"[{now.strftime('%H:%M:%S')}] "
                f"{p_occ:>5.3f}  {('Occupied' if occupied else 'Empty'):<8} | "
                f"{p_ly_str}  {p_st_str}  {p_mv_str}  {a_text:<8} | {DISPLAY[final_state]}",
                flush=True
            )

            if args.debug and transition:
                print(f"            transition: {transition}")

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        if csv_file is not None:
            csv_file.flush()
            csv_file.close()
        try:
            processor.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()