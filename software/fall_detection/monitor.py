import os
import queue
import socket
import threading
import time
import datetime

import numpy as np
import sounddevice as sd
from twilio.rest import Client
from flask import Flask, jsonify, render_template_string


# =========================
# 설정값
# =========================

LOUD_SOUND_THRESHOLD_DBFS = -0.0
LOUD_SOUND_RELEASE_THRESHOLD_DBFS = -25.0
LOUD_TO_LYING_WINDOW_SECONDS = 15.0
UDP_HOST = "127.0.0.1"
UDP_PORT = 5005
VALID_STATES = {"Empty", "Standing", "Moving", "Lying"}

CALL_ENABLED = os.environ.get("CALL_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
VOICE_TEMPLATE_URL = "https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech"


# =========================
# Twilio 환경 변수
# =========================

ALERT_PHONE_NUMBER = os.environ.get("ALERT_PHONE_NUMBER", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")


# =========================
# 공유 상태 (Dashboard 연동용)
# =========================

current_db = -120.0
current_state = "Unknown"
last_loud_sound_at = float("-inf")
last_alerted_loud_sound_at = float("-inf")
loud_input_active = False

# 낙상(전화 발신 조건 충족) 여부를 프론트엔드에 전달하기 위한 플래그
fall_detected_active = False

lock = threading.Lock()
call_queue = queue.Queue()

call_history = []
state_history = [] 


# =========================
# Flask 웹 서버 설정
# =========================

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>스마트 안전 모니터링</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.8/dist/web/static/pretendard.css" />
    <style>
        body { font-family: 'Pretendard', sans-serif; }
        
        .state-Unknown { background: linear-gradient(135deg, #94a3b8, #64748b); box-shadow: 0 4px 15px rgba(100, 116, 139, 0.3); }
        .state-Empty { background: linear-gradient(135deg, #cbd5e1, #94a3b8); box-shadow: 0 4px 15px rgba(148, 163, 184, 0.3); color: #333;}
        .state-Standing { background: linear-gradient(135deg, #60a5fa, #3b82f6); box-shadow: 0 4px 15px rgba(59, 130, 246, 0.4); }
        .state-Moving { background: linear-gradient(135deg, #34d399, #10b981); box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4); }
        .state-Lying { background: linear-gradient(135deg, #f87171, #ef4444); box-shadow: 0 0 25px rgba(239, 68, 68, 0.6); animation: dangerPulse 1.5s infinite; }
        
        .text-Unknown { color: #64748b; }
        .text-Empty { color: #94a3b8; }
        .text-Standing { color: #3b82f6; }
        .text-Moving { color: #10b981; }
        .text-Lying { color: #ef4444; }

        @keyframes dangerPulse {
            0% { transform: scale(1); box-shadow: 0 0 15px rgba(239, 68, 68, 0.5); }
            50% { transform: scale(1.02); box-shadow: 0 0 30px rgba(239, 68, 68, 0.8); }
            100% { transform: scale(1); box-shadow: 0 0 15px rgba(239, 68, 68, 0.5); }
        }

        @keyframes slideIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .log-item { animation: slideIn 0.3s ease-out forwards; }
        
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
    </style>
</head>
<body class="bg-gradient-to-br from-slate-50 to-slate-100 text-slate-800 p-4 md:p-8 min-h-screen flex flex-col items-center">
    
    <div class="max-w-5xl w-full">
        <div class="flex items-center justify-between mb-8 pb-4 border-b border-slate-200">
            <div>
                <h1 class="text-3xl font-extrabold text-slate-800 tracking-tight">안전 모니터링 시스템</h1>
                <p class="text-slate-500 mt-1 text-sm font-medium">실시간 활동 상태 및 긴급 호출 대시보드</p>
            </div>
            <div class="flex items-center gap-2 px-4 py-2 bg-white rounded-full shadow-sm border border-slate-100">
                <span class="relative flex h-3 w-3">
                  <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                  <span class="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
                </span>
                <span class="text-sm font-bold text-slate-600">시스템 정상 가동중</span>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            <div class="bg-white/80 backdrop-blur-md rounded-3xl shadow-lg shadow-slate-200/50 border border-white p-8 flex flex-col items-center justify-center transition-all">
                <p class="text-sm text-slate-500 font-bold mb-5 uppercase tracking-widest flex items-center gap-2">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                    현재 탐지 상태
                </p>
                <div id="state-badge" class="px-12 py-4 rounded-2xl text-4xl font-black text-white transition-all duration-500 state-Unknown text-center min-w-[200px]">
                    Unknown
                </div>
            </div>

            <div class="bg-white/80 backdrop-blur-md rounded-3xl shadow-lg shadow-slate-200/50 border border-white p-8 flex flex-col items-center justify-center">
                <p class="text-sm text-slate-500 font-bold mb-5 uppercase tracking-widest flex items-center gap-2">
                    <svg class="w-4 h-4 text-rose-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"></path></svg>
                    긴급 전화 발신 누적
                </p>
                <div class="text-6xl font-black text-rose-500 flex items-baseline drop-shadow-sm">
                    <span id="call-count">0</span><span class="text-2xl text-slate-400 font-bold ml-2">건</span>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="bg-white rounded-3xl shadow-sm border border-slate-100 p-6 flex flex-col">
                <div class="flex justify-between items-center mb-4 pb-2 border-b border-slate-50">
                    <h2 class="text-lg font-bold text-slate-700 flex items-center gap-2">
                        <svg class="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                        상태 변경 기록
                    </h2>
                    <span class="text-xs text-slate-400 font-medium bg-slate-100 px-2 py-1 rounded-md" id="state-log-count">0건</span>
                </div>
                <div id="state-history" class="space-y-3 h-64 overflow-y-auto pr-2">
                    <div class="flex items-center justify-center h-full text-slate-400 text-sm font-medium">기록이 없습니다.</div>
                </div>
            </div>

            <div class="bg-white rounded-3xl shadow-sm border border-slate-100 p-6 flex flex-col">
                <div class="flex justify-between items-center mb-4 pb-2 border-b border-slate-50">
                    <h2 class="text-lg font-bold text-slate-700 flex items-center gap-2">
                        <svg class="w-5 h-5 text-rose-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                        최근 발신 기록
                    </h2>
                </div>
                <div id="call-history" class="space-y-3 h-64 overflow-y-auto pr-2">
                    <div class="flex items-center justify-center h-full text-slate-400 text-sm font-medium">기록이 없습니다.</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let prevCallCount = -1;
        let prevStateCount = -1;
        let prevCombinedState = "";

        async function fetchData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();

                const combinedState = data.state + "_" + data.fall_detected;

                if (prevCombinedState !== combinedState) {
                    const badge = document.getElementById('state-badge');
                    
                    let displayText = data.state;
                    let textSize = "text-4xl";
                    
                    // 낙상 감지 상태일 때 텍스트 및 폰트 크기 덮어쓰기
                    if (data.state === "Lying" && data.fall_detected) {
                        displayText = "낙상 감지 됨";
                        textSize = "text-3xl"; 
                    }

                    badge.innerText = displayText;
                    // 클래스는 백엔드의 원본 state 값을 참조하여 배경색/애니메이션 유지
                    badge.className = `px-12 py-4 rounded-2xl ${textSize} font-black text-white transition-all duration-500 state-${data.state} text-center min-w-[200px]`;
                    
                    prevCombinedState = combinedState;
                }

                if (prevCallCount !== data.call_count) {
                    document.getElementById('call-count').innerText = data.call_count;
                    const callContainer = document.getElementById('call-history');
                    
                    if (data.call_history.length === 0) {
                        callContainer.innerHTML = '<div class="flex items-center justify-center h-full text-slate-400 text-sm font-medium">기록이 없습니다.</div>';
                    } else {
                        const reversedCall = [...data.call_history].reverse();
                        callContainer.innerHTML = reversedCall.map(log =>
                            `<div class="log-item flex items-center justify-between p-3 bg-rose-50/50 rounded-xl border border-rose-100">
                                <div class="flex items-center gap-3">
                                    <div class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></div>
                                    <span class="text-rose-700 font-bold tracking-wide">${log.time}</span>
                                </div>
                                <span class="text-xs font-bold text-rose-500 bg-rose-100 px-2 py-1 rounded-md">${log.status}</span>
                             </div>`
                        ).join('');
                    }
                    prevCallCount = data.call_count;
                }

                if (prevStateCount !== data.state_history.length) {
                    document.getElementById('state-log-count').innerText = `${data.state_history.length}건`;
                    const stateContainer = document.getElementById('state-history');

                    if (data.state_history.length === 0) {
                        stateContainer.innerHTML = '<div class="flex items-center justify-center h-full text-slate-400 text-sm font-medium">기록이 없습니다.</div>';
                    } else {
                        const reversedState = [...data.state_history].reverse();
                        stateContainer.innerHTML = reversedState.map(log =>
                            `<div class="log-item flex items-center justify-between p-3 bg-slate-50 hover:bg-slate-100 transition-colors rounded-xl border border-slate-100">
                                <span class="text-slate-500 font-bold text-sm bg-white shadow-sm border border-slate-100 px-2 py-1 rounded-md">${log.time}</span>
                                <div class="flex items-center gap-2 font-bold text-sm">
                                    <span class="text-${log.prev_state}">${log.prev_state}</span>
                                    <svg class="w-4 h-4 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
                                    <span class="text-${log.curr_state}">${log.curr_state}</span>
                                </div>
                             </div>`
                        ).join('');
                    }
                    prevStateCount = data.state_history.length;
                }

            } catch (e) {
                console.error('API 연동 오류:', e);
            }
        }
        
        setInterval(fetchData, 500);
        fetchData();
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/data")
def api_data():
    with lock:
        return jsonify({
            "state": current_state,
            "fall_detected": fall_detected_active,
            "call_count": len(call_history),
            "call_history": call_history,
            "state_history": state_history
        })


# =========================
# 백그라운드 로직 (Audio, UDP, Twilio)
# =========================

def audio_callback(indata, frames, time_info, status):
    global current_db, last_loud_sound_at, loud_input_active

    peak = float(np.max(np.abs(indata)))
    dbfs = 20 * np.log10(max(peak, 1e-7))
    now = time.monotonic()

    with lock:
        current_db = dbfs
        if dbfs >= LOUD_SOUND_THRESHOLD_DBFS:
            if not loud_input_active:
                last_loud_sound_at = now
                loud_input_active = True
        elif dbfs <= LOUD_SOUND_RELEASE_THRESHOLD_DBFS:
            loud_input_active = False

def receive_external_states():
    global current_state, last_alerted_loud_sound_at, fall_detected_active

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((UDP_HOST, UDP_PORT))
        while True:
            data, _ = sock.recvfrom(1024)
            try:
                state = data.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue

            if state not in VALID_STATES:
                continue

            now = time.monotonic()
            should_place_call = False

            with lock:
                if current_state != state:
                    record_time = datetime.datetime.now().strftime("%H:%M:%S")
                    state_history.append({
                        "time": record_time,
                        "prev_state": current_state,
                        "curr_state": state
                    })
                    if len(state_history) > 50:
                        state_history.pop(0)
                        
                    # Lying 상태를 벗어나면 낙상 감지 플래그 리셋
                    if state != "Lying":
                        fall_detected_active = False
                        
                current_state = state

                if state == "Lying":
                    within_time_window = (now - last_loud_sound_at <= LOUD_TO_LYING_WINDOW_SECONDS)
                    not_alerted = (last_loud_sound_at > last_alerted_loud_sound_at)

                    if within_time_window and not_alerted:
                        last_alerted_loud_sound_at = last_loud_sound_at
                        should_place_call = True
                        fall_detected_active = True  # 낙상 감지 플래그 활성화

            if should_place_call and CALL_ENABLED:
                call_queue.put(None)

def voice_call_worker():
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    while True:
        call_queue.get()
        try:
            call = client.calls.create(
                to=ALERT_PHONE_NUMBER,
                from_=TWILIO_FROM_NUMBER,
                url=VOICE_TEMPLATE_URL,
            )
            record_time = datetime.datetime.now().strftime("%H:%M:%S")
            with lock:
                call_history.append({"time": record_time, "status": "발신 성공"})
            print(f"[전화 발신 성공] Call SID: {call.sid}")
        except Exception as error:
            record_time = datetime.datetime.now().strftime("%H:%M:%S")
            with lock:
                call_history.append({"time": record_time, "status": "발신 실패"})
            print(f"[전화 발신 실패] {error}")
        finally:
            call_queue.task_done()

def validate_call_settings():
    if not CALL_ENABLED:
        return
    missing = [key for key, val in {
        "ALERT_PHONE_NUMBER": ALERT_PHONE_NUMBER,
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_FROM_NUMBER": TWILIO_FROM_NUMBER
    }.items() if not val]
    if missing:
        raise RuntimeError("전화 환경 변수가 비어 있습니다: " + ", ".join(missing))


# =========================
# 메인 실행부
# =========================

def main():
    validate_call_settings()

    threading.Thread(target=receive_external_states, daemon=True).start()

    if CALL_ENABLED:
        threading.Thread(target=voice_call_worker, daemon=True).start()

    stream = sd.InputStream(
        channels=1,
        samplerate=44100,
        blocksize=256,
        latency="low",
        callback=audio_callback,
    )
    stream.start()

    print("=========================================")
    print(" 시스템이 시작되었습니다.")
    print(" 웹 브라우저를 열고 http://127.0.0.1:5000 에 접속하세요.")
    print("=========================================")

    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
