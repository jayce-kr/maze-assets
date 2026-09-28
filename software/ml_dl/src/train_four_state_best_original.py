# train_four_state_best_original.py
from __future__ import annotations
import argparse, gc, random, re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, average_precision_score, balanced_accuracy_score,
    f1_score, matthews_corrcoef, precision_score, recall_score, roc_auc_score,
    confusion_matrix
)
from sklearn.utils.class_weight import compute_class_weight, compute_sample_weight
from xgboost import XGBClassifier

import matplotlib.pyplot as plt
import seaborn as sns

# ============================================================================
# Fixed configuration
# ============================================================================
FS = 40.0
N_RAW_SC = 64
EPS = 1e-6
REMOVE_SC = [0, 1, 2, 3, 32, 33, 61, 62, 63]
VALID_SC = np.asarray(sorted(set(range(N_RAW_SC)) - set(REMOVE_SC)), dtype=np.int32)
PRESENCE_WINDOW = 400     # 10 s
ACTIVITY_WINDOW = 120     # 3 s
SLIDE = 40                # 1 s
BASELINE_SEC = 30.0
BASELINE_PACKETS = int(BASELINE_SEC * FS)

# 4상태 폴더 맵핑
FOLDERS = {
    "empty": "empty", 
    "p1_lying": "lying", 
    "p1_standing": "standing", 
    "p1_moving": "moving"
}

@dataclass(frozen=True)
class FileMeta:
    path: Path
    label: str
    person: str
    session: str
    pos: str
    timestamp: Optional[datetime]

    @property
    def file_id(self) -> str:
        return str(self.path.resolve())

    @property
    def exact_group(self) -> str:
        return f"{self.person}_{self.session}_{self.pos}"

def parse_timestamp(stem: str) -> Optional[datetime]:
    m = re.search(r"(\d{8})_(\d{6})(?:_|$)", stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None

def parse_file(path: Path, label: str) -> Optional[FileMeta]:
    parts = path.stem.split("_")
    if len(parts) < 5 or parts[0] != "session":
        return None
    if not re.fullmatch(r"pos\d+", parts[4]):
        return None
    return FileMeta(
        path=path, label=label, person=parts[2], session=parts[3], pos=parts[4],
        timestamp=parse_timestamp(path.stem)
    )

def find_files(root: Path) -> List[FileMeta]:
    out = []
    for folder, label in FOLDERS.items():
        d = root / folder
        if not d.exists():
            raise RuntimeError(f"Required folder missing: {d}")
        for p in sorted(d.rglob("*.npz")):
            m = parse_file(p, label)
            if m is not None:
                out.append(m)
    if not out:
        raise RuntimeError(f"No valid NPZ files under {root}")
    return out

def load_amp_rssi(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    with np.load(path, allow_pickle=True) as d:
        if "csi_iq" not in d:
            raise KeyError(f"{path}: csi_iq missing")
        csi = np.asarray(d["csi_iq"], dtype=np.float32)
        if "rssi" in d:
            rssi = np.asarray(d["rssi"], dtype=np.float32).reshape(-1)
        elif "rssis" in d:
            rssi = np.asarray(d["rssis"], dtype=np.float32).reshape(-1)
        else:
            rssi = np.zeros(len(csi), dtype=np.float32)

    if csi.ndim != 3 or csi.shape[-1] != 2:
        raise ValueError(f"{path}: invalid CSI shape {csi.shape}")

    n, n_sc, _ = csi.shape
    if n_sc < N_RAW_SC:
        csi = np.concatenate(
            [csi, np.zeros((n, N_RAW_SC - n_sc, 2), dtype=np.float32)], axis=1
        )
    elif n_sc > N_RAW_SC:
        csi = csi[:, :N_RAW_SC, :]

    if len(rssi) < n:
        rssi = np.pad(rssi, (0, n-len(rssi)), mode="edge") if len(rssi) else np.zeros(n, np.float32)
    elif len(rssi) > n:
        rssi = rssi[:n]

    I = np.fft.fftshift(csi[..., 0], axes=1)[:, VALID_SC]
    Q = np.fft.fftshift(csi[..., 1], axes=1)[:, VALID_SC]
    amp = np.sqrt(I*I + Q*Q).astype(np.float32)
    return amp, rssi.astype(np.float32)

def choose_nearest_empty(target: FileMeta, candidates: Sequence[FileMeta]) -> FileMeta:
    valid = [x for x in candidates if x.timestamp is not None]
    if target.timestamp is not None and valid:
        return min(valid, key=lambda x: abs((x.timestamp-target.timestamp).total_seconds()))
    return sorted(candidates, key=lambda x: x.path.name)[0]

def build_baseline_map(metas: Sequence[FileMeta]) -> Dict[str, FileMeta]:
    by_group: Dict[str, List[FileMeta]] = {}
    for m in metas:
        if m.label == "empty":
            by_group.setdefault(m.exact_group, []).append(m)

    mapping = {}
    for m in metas:
        if m.label == "empty":
            mapping[m.file_id] = m
        else:
            c = by_group.get(m.exact_group, [])
            if c:
                mapping[m.file_id] = choose_nearest_empty(m, c)
    return mapping

def build_baseline_cache(metas, mapping):
    lookup = {m.file_id: m for m in metas if m.label == "empty"}
    ids = sorted({x.file_id for x in mapping.values()})
    amp_cache, rssi_cache = {}, {}
    print("\nBuilding matched Empty baselines...")
    for i, eid in enumerate(ids, 1):
        amp, rssi = load_amp_rssi(lookup[eid].path)
        n = min(len(amp), BASELINE_PACKETS)
        if n < 100:
            raise RuntimeError(f"Too few Empty packets: {lookup[eid].path}")
        amp_cache[eid] = np.median(amp[:n], axis=0).astype(np.float32)
        rssi_cache[eid] = float(np.median(rssi[:n]))
        if i % 10 == 0 or i == len(ids):
            print(f"  {i}/{len(ids)}")
    return amp_cache, rssi_cache

def uniform_starts(n_packets, window, max_windows, start0=0):
    if n_packets < start0 + window:
        return np.empty(0, np.int32)
    s = np.arange(start0, n_packets-window+1, SLIDE, dtype=np.int32)
    if max_windows > 0 and len(s) > max_windows:
        idx = np.unique(np.rint(np.linspace(0, len(s)-1, max_windows)).astype(np.int32))
        s = s[idx]
    return s

def file_level_split(pool_idx, labels, persons, file_ids, val_ratio, seed):
    rng = np.random.default_rng(seed)
    pp, yy, ff = persons[pool_idx], labels[pool_idx], file_ids[pool_idx]
    train_ids, val_ids = set(), set()

    for person in sorted(set(pp.tolist())):
        for label in sorted(set(yy.tolist())):
            mask = (pp == person) & (yy == label)
            files = np.unique(ff[mask]).copy()
            if not len(files):
                continue
            rng.shuffle(files)
            if len(files) <= 1:
                n_val = 0
            else:
                n_val = max(1, int(round(len(files)*val_ratio)))
                n_val = min(n_val, len(files)-1)
            val_ids.update(files[:n_val].tolist())
            train_ids.update(files[n_val:].tolist())

    tr = pool_idx[np.asarray([f in train_ids for f in ff], dtype=bool)]
    va = pool_idx[np.asarray([f in val_ids for f in ff], dtype=bool)]
    if len(va) == 0:
        tmp = pool_idx.copy()
        rng.shuffle(tmp)
        n_val = max(1, int(round(len(tmp)*val_ratio)))
        va, tr = tmp[:n_val], tmp[n_val:]
    return tr, va

def tune_threshold(y, scores):
    lo, hi = float(np.min(scores)), float(np.max(scores))
    if abs(hi-lo) < 1e-12:
        return lo
    best_th, best_bacc, best_gap = 0.5, -1.0, 999.0
    for th in np.linspace(lo, hi, 401):
        p = (scores >= th).astype(np.int32)
        b = balanced_accuracy_score(y, p)
        r0 = recall_score(y, p, pos_label=0, zero_division=0)
        r1 = recall_score(y, p, pos_label=1, zero_division=0)
        gap = abs(r0-r1)
        if b > best_bacc + 1e-12 or (abs(b-best_bacc) <= 1e-12 and gap < best_gap):
            best_th, best_bacc, best_gap = float(th), float(b), float(gap)
    return best_th

def metrics(y, scores, th, neg_name, pos_name):
    p = (scores >= th).astype(np.int32)
    try:
        auc = float(roc_auc_score(y, scores))
    except Exception:
        auc = np.nan
    try:
        pr = float(average_precision_score(y, scores))
    except Exception:
        pr = np.nan
    return {
        "accuracy": float(accuracy_score(y, p)),
        "balanced_accuracy": float(balanced_accuracy_score(y, p)),
        f"{neg_name}_recall": float(recall_score(y, p, pos_label=0, zero_division=0)),
        f"{pos_name}_recall": float(recall_score(y, p, pos_label=1, zero_division=0)),
        f"{pos_name}_precision": float(precision_score(y, p, pos_label=1, zero_division=0)),
        "f1": float(f1_score(y, p, zero_division=0)),
        "mcc": float(matthews_corrcoef(y, p)),
        "roc_auc": auc,
        "pr_auc": pr,
    }

def metrics_multiclass(y, p, class_names):
    m = {
        "accuracy": float(accuracy_score(y, p)),
        "balanced_accuracy": float(balanced_accuracy_score(y, p)),
        "f1_macro": float(f1_score(y, p, average="macro", zero_division=0)),
    }
    recalls = recall_score(y, p, average=None, labels=range(len(class_names)), zero_division=0)
    for i, name in enumerate(class_names):
        m[f"{name}_recall"] = float(recalls[i])
    return m

def plot_training_history(history, save_path):
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    ax[0].plot(history['loss'], label='Train Loss')
    if 'val_loss' in history:
        ax[0].plot(history['val_loss'], label='Val Loss')
    ax[0].set_title('Model Loss')
    ax[0].set_xlabel('Epoch')
    ax[0].set_ylabel('Loss')
    ax[0].legend()
    
    if 'accuracy' in history:
        ax[1].plot(history['accuracy'], label='Train Accuracy')
        if 'val_accuracy' in history:
            ax[1].plot(history['val_accuracy'], label='Val Accuracy')
        ax[1].set_title('Model Accuracy')
        ax[1].set_xlabel('Epoch')
        ax[1].set_ylabel('Accuracy')
        ax[1].legend()

    plt.tight_layout()
    plt.savefig(save_path)
    plt.close()

def plot_cm(y_true, y_pred, labels, title, save_path):
    cm = confusion_matrix(y_true, y_pred)
    plt.figure(figsize=(6, 5))
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', 
                xticklabels=labels, yticklabels=labels)
    plt.title(title)
    plt.ylabel('True Label')
    plt.xlabel('Predicted Label')
    plt.tight_layout()
    plt.savefig(save_path)
    plt.close()

def temporal_zscore(x):
    mu = np.mean(x, axis=0, keepdims=True)
    sd = np.std(x, axis=0, keepdims=True)
    z = (x-mu)/(sd+EPS)
    return np.clip(np.nan_to_num(z, nan=0.0, posinf=8.0, neginf=-8.0), -8, 8).astype(np.float32)

def relative_delta(x, baseline):
    rel = (x-baseline[None, :])/(np.abs(baseline[None, :])+EPS)
    return np.clip(np.nan_to_num(rel, nan=0.0, posinf=5.0, neginf=-5.0), -5, 5).astype(np.float32)

def presence_rep(x, baseline):
    return np.concatenate([temporal_zscore(x), relative_delta(x, baseline)], axis=1).astype(np.float32)

def build_presence_dataset(metas, mapping, amp_baselines, max_windows):
    X, y, persons, files, source, starts_out = [], [], [], [], [], []
    counts = {"empty": 0, "lying": 0, "standing": 0, "moving": 0}
    skipped = 0
    print("\nBuilding Presence dataset...")
    for i, m in enumerate(metas, 1):
        if m.file_id not in mapping:
            skipped += 1
            continue
        amp, _ = load_amp_rssi(m.path)
        base = amp_baselines[mapping[m.file_id].file_id]
        start0 = BASELINE_PACKETS if m.label == "empty" else 0
        starts = uniform_starts(len(amp), PRESENCE_WINDOW, max_windows, start0)
        for s in starts:
            X.append(presence_rep(amp[s:s+PRESENCE_WINDOW], base).astype(np.float16))
            y.append(0 if m.label == "empty" else 1)
            persons.append(m.person)
            files.append(m.file_id)
            source.append(m.label)
            starts_out.append(int(s))
            counts[m.label] += 1
        if i % 20 == 0 or i == len(metas):
            print(f"  {i}/{len(metas)}")
    if not X:
        raise RuntimeError("No Presence windows generated.")
    X = np.stack(X)
    y = np.asarray(y, np.int32)
    persons, files, source = np.asarray(persons), np.asarray(files), np.asarray(source)
    starts_out = np.asarray(starts_out, np.int32)
    print(f"Presence X={X.shape} | Empty={np.sum(y==0)} Occupied={np.sum(y==1)}")
    print("Source windows:", counts, "| skipped files:", skipped)
    return X, y, persons, files, source, starts_out

def import_tf():
    try:
        import tensorflow as tf
    except Exception as e:
        raise ImportError("TensorFlow required: pip install tensorflow") from e
    return tf
    
def make_presence_cnn(tf, seed):
    tf.keras.utils.set_random_seed(seed)
    inp = tf.keras.Input(shape=(PRESENCE_WINDOW, len(VALID_SC)*2))
    x = tf.keras.layers.Conv1D(64, 7, padding="same", activation="relu")(inp)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.MaxPooling1D(2)(x)
    x = tf.keras.layers.Conv1D(128, 5, padding="same", activation="relu")(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.MaxPooling1D(2)(x)
    x = tf.keras.layers.Conv1D(128, 3, padding="same", activation="relu")(x)
    x = tf.keras.layers.GlobalAveragePooling1D()(x)
    x = tf.keras.layers.Dropout(0.30)(x)
    out = tf.keras.layers.Dense(1, activation="sigmoid")(x)
    model = tf.keras.Model(inp, out, name="presence_cnn1d")
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3),
                  loss="binary_crossentropy", metrics=["accuracy"])
    return model

def class_weights(y):
    w = compute_class_weight(class_weight="balanced", classes=np.array([0,1]), y=y)
    return {0: float(w[0]), 1: float(w[1])}

def fit_presence(X, y, tr, va, seed, epochs, batch, verbose):
    tf = import_tf()
    tf.keras.backend.clear_session()
    model = make_presence_cnn(tf, seed)
    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor="val_loss", patience=5, min_delta=1e-4, restore_best_weights=True),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5, patience=2, min_lr=1e-5, verbose=0),
    ]
    h = model.fit(
        np.asarray(X[tr], np.float32), y[tr],
        validation_data=(np.asarray(X[va], np.float32), y[va]),
        epochs=epochs, batch_size=batch, class_weight=class_weights(y[tr]),
        callbacks=callbacks, shuffle=True, verbose=verbose
    )
    s = model.predict(np.asarray(X[va], np.float32),
                      batch_size=batch, verbose=0).reshape(-1)
    return model, s, h.history

def presence_loso(X, y, persons, files, val_ratio, seed, epochs, batch, verbose):
    rows = []
    print("\n" + "="*90 + "\nPRESENCE LOSO\n" + "="*90)
    for k, test_person in enumerate(sorted(set(persons.tolist())), 1):
        te = np.flatnonzero(persons == test_person)
        pool = np.flatnonzero(persons != test_person)
        tr, va = file_level_split(pool, y, persons, files, val_ratio, seed+k)
        model, vs, hist = fit_presence(X, y, tr, va, seed+k, epochs, batch, verbose)
        ep = len(hist["loss"])
        th = tune_threshold(y[va], vs)
        ts = model.predict(np.asarray(X[te], np.float32), batch_size=batch, verbose=0).reshape(-1)
        m = metrics(y[te], ts, th, "empty", "occupied")
        rows.append({"test_person": test_person, "threshold": th, "epochs_ran": ep, **m})
        print(f"{test_person}: BAcc={m['balanced_accuracy']*100:.2f}% "
              f"EmptyR={m['empty_recall']*100:.2f}% "
              f"OccR={m['occupied_recall']*100:.2f}% th={th:.3f}")
        del model
        import_tf().keras.backend.clear_session()
        gc.collect()
    return pd.DataFrame(rows)

def train_final_presence(X, y, persons, files, source, starts, args, out, loso):
    idx = np.arange(len(y), dtype=np.int32)
    tr, va = file_level_split(idx, y, persons, files, args.val_ratio, args.seed)
    model, vs, hist = fit_presence(
        X, y, tr, va, args.seed, args.epochs, args.batch_size, args.verbose
    )
    ep = len(hist["loss"])
    th = tune_threshold(y[va], vs)
    m = metrics(y[va], vs, th, "empty", "occupied")
    model.save(out / "presence_cnn.keras")

    meta = {
        "version": "four_state_presence_original_v1",
        "fs": FS, "window": PRESENCE_WINDOW, "window_sec": PRESENCE_WINDOW/FS,
        "slide": SLIDE, "baseline_sec": BASELINE_SEC, "baseline_packets": BASELINE_PACKETS,
        "remove_subcarriers": REMOVE_SC, "valid_subcarriers": VALID_SC.tolist(),
        "n_raw_subcarriers": N_RAW_SC, "n_clean_subcarriers": len(VALID_SC),
        "input_channels": len(VALID_SC)*2,
        "representation": "temporal_zscore_55_plus_relative_to_empty_55",
        "rssi_used": False, "phase_used": False,
        "occupied_threshold": float(th), "validation_metrics": m,
        "epochs_ran": int(ep),
        "deployment_assumption": "30-second Empty calibration before inference",
    }
    if loso is not None:
        meta["loso_mean"] = {
            c: float(loso[c].mean()) for c in
            ["accuracy","balanced_accuracy","empty_recall","occupied_recall",
             "f1","mcc","roc_auc","pr_auc"]
        }
    joblib.dump(meta, out / "presence_meta.joblib")
    pred = (vs >= th).astype(np.int32)
    pd.DataFrame({
        "y_true": y[va], "p_occupied": vs, "prediction": pred,
        "person": persons[va], "source_label": source[va],
        "file_id": files[va], "window_start_packet": starts[va],
    }).to_csv(out / "presence_validation.csv", index=False, encoding="utf-8-sig")
    
    print("\nFinal Presence:")
    print(f"threshold={th:.4f} | BAcc={m['balanced_accuracy']*100:.2f}% | "
          f"EmptyR={m['empty_recall']*100:.2f}% | OccR={m['occupied_recall']*100:.2f}%")
          
    plot_training_history(hist, out / "presence_training_history.png")
    plot_cm(y[va], pred, ["Empty", "Occupied"], "Presence CNN Validation CM", out / "presence_cm.png")

def sc_stats(x):
    d = np.diff(x, axis=0)
    ad = np.abs(d)
    xmax, xmin = np.max(x, axis=0), np.min(x, axis=0)
    return np.concatenate([
        np.mean(x, axis=0), np.std(x, axis=0), xmax, xmin,
        np.median(x, axis=0), xmax-xmin,
        np.std(d, axis=0), np.mean(ad, axis=0), np.percentile(ad, 95, axis=0)
    ]).astype(np.float32)

def activity_feature(amp, rssi, baseline_rssi):
    dr = rssi.astype(np.float32) - float(baseline_rssi)
    rf = np.array(
        [np.mean(dr), np.std(dr), np.min(dr), np.max(dr), np.max(dr)-np.min(dr)],
        dtype=np.float32
    )
    return np.concatenate([sc_stats(amp), rf]).astype(np.float32)

def build_activity_dataset(metas, mapping, rssi_baselines, max_windows):
    X, y, persons, files = [], [], [], []
    counts = {"lying":0, "standing":0, "moving":0}
    label_map = {"lying": 0, "standing": 1, "moving": 2}
    
    act = [m for m in metas if m.label in {"lying", "standing", "moving"}]
    print("\nBuilding 3-second Activity dataset (Lying, Standing, Moving)...")
    skipped = 0
    for i, m in enumerate(act, 1):
        if m.file_id not in mapping:
            skipped += 1
            continue
        amp, rssi = load_amp_rssi(m.path)
        starts = uniform_starts(len(amp), ACTIVITY_WINDOW, max_windows, 0)
        base_rssi = rssi_baselines[mapping[m.file_id].file_id]
        for s in starts:
            X.append(activity_feature(
                amp[s:s+ACTIVITY_WINDOW], rssi[s:s+ACTIVITY_WINDOW], base_rssi))
            y.append(label_map[m.label])
            persons.append(m.person)
            files.append(m.file_id)
            counts[m.label] += 1
        if i % 20 == 0 or i == len(act):
            print(f"  {i}/{len(act)}")
    X = np.stack(X).astype(np.float32)
    y = np.asarray(y, np.int32)
    persons, files = np.asarray(persons), np.asarray(files)
    print(f"Activity X={X.shape} | Lying={counts['lying']} Standing={counts['standing']} Moving={counts['moving']} "
          f"| skipped files={skipped}")
    return X, y, persons, files

def make_xgb(seed):
    return XGBClassifier(
        n_estimators=350, max_depth=4, learning_rate=0.05,
        subsample=0.85, colsample_bytree=0.85,
        objective="multi:softprob", num_class=3, eval_metric="mlogloss",
        tree_method="hist", n_jobs=-1, random_state=seed
    )

def activity_loso(X, y, persons, files, val_ratio, seed):
    rows = []
    print("\n" + "="*90 + "\nACTIVITY 3s XGBOOST LOSO (MULTI-CLASS)\n" + "="*90)
    for k, test_person in enumerate(sorted(set(persons.tolist())), 1):
        te = np.flatnonzero(persons == test_person)
        pool = np.flatnonzero(persons != test_person)
        tr, va = file_level_split(pool, y, persons, files, val_ratio, seed+100+k)
        model = make_xgb(seed+100+k)
        weights = compute_sample_weight(class_weight='balanced', y=y[tr])
        model.fit(X[tr], y[tr], sample_weight=weights)
        
        ts = model.predict(X[te])
        m = metrics_multiclass(y[te], ts, ["lying", "standing", "moving"])
        rows.append({"test_person": test_person, **m})
        
        print(f"{test_person}: BAcc={m['balanced_accuracy']*100:.2f}% "
              f"LyingR={m['lying_recall']*100:.2f}% "
              f"StandingR={m['standing_recall']*100:.2f}% "
              f"MovingR={m['moving_recall']*100:.2f}%")
    return pd.DataFrame(rows)

def train_final_activity(X, y, persons, files, args, out, loso):
    idx = np.arange(len(y), dtype=np.int32)
    tr, va = file_level_split(idx, y, persons, files, args.val_ratio, args.seed+500)
    model = make_xgb(args.seed)
    weights = compute_sample_weight(class_weight='balanced', y=y[tr])
    model.fit(X[tr], y[tr], sample_weight=weights)
    
    vs = model.predict(X[va])
    probs = model.predict_proba(X[va])
    
    m = metrics_multiclass(y[va], vs, ["lying", "standing", "moving"])
    
    bundle = {
        "model": model, 
        "classes": {0: "Lying", 1: "Standing", 2: "Moving"},
        "info": {
            "window": ACTIVITY_WINDOW, "window_sec": ACTIVITY_WINDOW/FS,
            "slide": SLIDE, "fs": FS, "valid_subcarriers": VALID_SC.tolist(),
            "feature_count": int(X.shape[1]),
            "feature_type": "55SC_x_9_stats_plus_5_RSSI_delta",
            "rssi_baseline_sec": BASELINE_SEC,
        }
    }
    joblib.dump(bundle, out / "activity_xgb.pkl")
    meta = {
        "version": "four_state_activity_xgb_3s_original_v1",
        "validation_metrics": m,
        "window": ACTIVITY_WINDOW, "window_sec": ACTIVITY_WINDOW/FS,
        "slide": SLIDE, "fs": FS, "feature_count": int(X.shape[1]),
        "recommended_realtime_hysteresis": {
            "to_moving_probability": 0.80, "to_moving_count": 3,
            "to_standing_probability": 0.70, "to_standing_count": 3,
            "to_lying_probability": 0.70, "to_lying_count": 3,
        },
    }
    if loso is not None:
        meta["loso_mean"] = {
            c: float(loso[c].mean()) for c in
            ["accuracy", "balanced_accuracy", "f1_macro", 
             "lying_recall", "standing_recall", "moving_recall"]
        }
    joblib.dump(meta, out / "activity_meta.joblib")
    
    pd.DataFrame({
        "y_true": y[va], "prediction": vs,
        "p_lying": probs[:, 0], "p_standing": probs[:, 1], "p_moving": probs[:, 2],
        "person": persons[va], "file_id": files[va],
    }).to_csv(out / "activity_validation.csv", index=False, encoding="utf-8-sig")
    
    print("\nFinal Activity:")
    print(f"BAcc={m['balanced_accuracy']*100:.2f}% | "
          f"LyingR={m['lying_recall']*100:.2f}% | "
          f"StandingR={m['standing_recall']*100:.2f}% | "
          f"MovingR={m['moving_recall']*100:.2f}%")
          
    plot_cm(y[va], vs, ["Lying", "Standing", "Moving"], "Activity XGB Validation CM", out / "activity_cm.png")

# ============================================================================
# Main
# ============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", required=True)
    ap.add_argument("--output_dir", default=r".\models\four_state_best_original")
    ap.add_argument("--presence_windows_per_file", type=int, default=20)
    ap.add_argument("--activity_windows_per_file", type=int, default=20)
    ap.add_argument("--val_ratio", type=float, default=0.20)
    ap.add_argument("--epochs", type=int, default=35)
    ap.add_argument("--batch_size", type=int, default=32)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--verbose", type=int, default=1, choices=[0,1,2])
    ap.add_argument("--skip_loso", action="store_true")
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    root = Path(args.input_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    metas = find_files(root)
    print("\n" + "="*105)
    print("FOUR-STATE BEST ORIGINAL-DATA TRAINER")
    print("="*105)
    print("Uses ONLY: empty / p1_lying / p1_standing / p1_moving")
    print("Files:", {k:sum(m.label==k for m in metas) for k in ["empty", "lying", "standing", "moving"]})
    print("Persons:", sorted(set(m.person for m in metas)))
    print(f"Presence: 10 s CNN, Activity: 3 s XGBoost, slide: 1 s")

    mapping = build_baseline_map(metas)
    print(f"Matched Empty baseline map: {len(mapping)}/{len(metas)} files")
    amp_base, rssi_base = build_baseline_cache(metas, mapping)

    Xp, yp, pp, fp, srcp, starts = build_presence_dataset(
        metas, mapping, amp_base, args.presence_windows_per_file
    )

    ploso = None
    if not args.skip_loso:
        ploso = presence_loso(
            Xp, yp, pp, fp, args.val_ratio, args.seed,
            args.epochs, args.batch_size, args.verbose
        )
        ploso.to_csv(out/"presence_loso.csv", index=False, encoding="utf-8-sig")
        print("\nPresence LOSO mean:",
              f"BAcc={ploso['balanced_accuracy'].mean()*100:.2f}% |",
              f"EmptyR={ploso['empty_recall'].mean()*100:.2f}% |",
              f"OccR={ploso['occupied_recall'].mean()*100:.2f}%")

    train_final_presence(Xp, yp, pp, fp, srcp, starts, args, out, ploso)
    del Xp
    gc.collect()

    Xa, ya, pa, fa = build_activity_dataset(
        metas, mapping, rssi_base, args.activity_windows_per_file
    )

    aloso = None
    if not args.skip_loso:
        aloso = activity_loso(Xa, ya, pa, fa, args.val_ratio, args.seed)
        aloso.to_csv(out/"activity_loso.csv", index=False, encoding="utf-8-sig")
        print("\nActivity LOSO mean:",
              f"BAcc={aloso['balanced_accuracy'].mean()*100:.2f}% |",
              f"LyingR={aloso['lying_recall'].mean()*100:.2f}% |",
              f"StandingR={aloso['standing_recall'].mean()*100:.2f}% |",
              f"MovingR={aloso['moving_recall'].mean()*100:.2f}%")

    train_final_activity(Xa, ya, pa, fa, args, out, aloso)

    print("\nSaved:")
    for name in ["presence_cnn.keras", "presence_meta.joblib", "activity_xgb.pkl", "activity_meta.joblib", "presence_training_history.png", "presence_cm.png", "activity_cm.png"]:
        print(" ", out/name)

if __name__ == "__main__":
    main()