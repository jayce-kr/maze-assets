/*
	- 보드 레이트를 921600으로 올림
	- 학습 정확도를 높이기 위해 10Hz -> 40Hz로 올림.
		(패킷 드랍 감수 -> 조정 필요할 수 있음.)
	- 타 Wi-Fi를 고려하여 채널을 11로 이동.
*/
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

// 브로드 캐스트 주소 (모든 수신자에게 전송)
uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

void setup() {
  Serial.begin(921600);

  /*
    WiFi.mode(WIFI_STA);를 호출해야만 비로소 와이파이 하드웨어에 전원이 공급되고, 통신에 필요한 소프트웨어 엔진(스택)이 메모리에 올라옴.
    이 과정이 없으면 패킷을 쏘거나 엿듣는(Sniffing) 하위 레벨 기능 자체가 동작X.
      - WIFI_STA: ESP32가 다른 와이파이에 연결되는 모드. (Station 모드)
      - WIFI_AP: ESP32가 자체적으로 와이파이 신호를 뿜어내는 모드. (Access Point 모드)
      - WIFI_AP_STA: 두 모드를 동시에 지원하는 모드. (Station + Access Point 모드)
  */
  WiFi.mode(WIFI_STA);

  /*
    ESP32는 WIFI_STA 모드에서 이전에 연결했던 공유기 정보(SSID, Password)가 메모리에 남아 있으면, 전원이 켜지자마자 자동으로 그 공유기를 찾아 연결하려고 시도함.
    이 과정에서 ESP32는 다른 채널들을 돌아다니며 '스캔'을 하게 되는데, 우리가 특정 채널에서 CSI 데이터를 수집하고 있을 때 이 스캔 작업이 끼어들면 데이터가 끊기거나 유실될 수 있음.

    CSI 추출을 위해서는 Tx(송신기)와 Rx(수신기)가 똑같은 채널에 고정되어 있어야 함.
    그런데 WiFi.disconnect();를 해주지 않으면, 내부 로직이 연결을 시도하느라 채널을 계속 변경하거나, 연결 실패 시 재시도 모드로 들어가면서 우리가 설정한 채널 설정을 덮어버릴 수 있음.

    우리가 사용하려는 esp_wifi_set_promiscuous(true) 함수는 하위 레벨의 패킷을 가로채는 기능.
    이 기능은 와이파이 스택이 어딘가에 연결되어 '바쁜' 상태일 때보다, 어디에도 묶여 있지 않은 '자유로운(Disconnected)' 상태일 때 훨씬 안정적으로 동작.

      - WiFi.disconnect()는 ESP32가 현재 연결된 Wi-Fi 네트워크에서 연결을 끊는 함수.
      - wifioff 매개변수를 true로 설정하면, Wi-Fi 하드웨어의 전원을 완전히 차단하여, ESP32가 Wi-Fi 기능을 일시적으로 사용할 수 없도록 함.
      - wifioff 매개변수를 false로 설정하면, Wi-Fi 하드웨어의 전원은 유지하지만, 현재 연결된 네트워크에서 연결만 끊음.
  */
  WiFi.disconnect();

  /*
    802.11b/g (구형): 부반송파 개수가 적거나 CSI 데이터를 정밀하게 추출하기 어려움.
    802.11n (HT 모드): OFDM 기술을 사용하여 20MHz 대역폭 기준으로 총 64개의 부반송파 정보를 제공.
    => 즉, esp_wifi_set_protocol() 함수를 사용하여 Wi-Fi 프로토콜을 802.11n으로 고정.
       이렇게 해야만 ESP-NOW 패킷이 802.11n 형식으로 전송되고, Rx 보드가 이를 인식하여 CSI 데이터를 뱉어냄.

     - WIFI_IF_STA: Station 인터페이스에 대한 프로토콜 설정
     - WIFI_PROTOCOL_11B: 802.11b 프로토콜 지원
     - WIFI_PROTOCOL_11G: 802.11g 프로토콜 지원
     - WIFI_PROTOCOL_11N: 802.11n 프로토콜 지원
     - WIFI_PROTOCOL_LR: 장거리 통신을 위한 프로토콜 지원

    만약 이 설정을 하지 않으면, ESP32는 주변 환경에 따라 자동으로 802.11b나 11g로 모드를 낮출 수도 있는데,
    이렇게 갑자기 11g로 모드가 바뀌면 수집되는 CSI 데이터의 포맷이 변하거나, 아예 데이터가 들어오지 않는 버그가 발생할 수 있음.
    => set_protocol을 통해 11n으로 고정함으로써 실험 내내 동일한 규격의 고품질 데이터를 얻을 수 있게 함.
  */
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11N);
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
  /*
    WIFI_STA 모드에서 공유기에 연결되지 않은 상태라면, 드라이버는 내가 어디에 연결될지 모르니 채널을 마음대로 고정하지 않음.
    하지만 CSI 데이터를 수집하려면 Tx와 Rx가 똑같은 채널에 고정되어 있어야 함.
    그래서 esp_wifi_set_promiscuous(true) 함수를 사용하여 Promiscuous Mode를 활성화하고, esp_wifi_set_channel() 함수를 사용하여 채널 1번으로 고정.

    Promiscuous Mode(무차별 수신 모드)
    : ESP32가 자신에게 도착하는 패킷뿐만 아니라, 같은 채널에 있는 모든 패킷을 가로채서 처리할 수 있게 하는 모드.
    이 모드를 활성화하면, ESP32가 특정 채널에 고정되어 그 채널에서 오가는 모든 패킷을 수집할 수 있음.

    왜 필요한가?
    두 기기가 연결되지 않았기 때문에, Tx(송신기)가 쏘는 패킷에는 Rx(수신기)의 주소가 적혀 있지 않음.
    만약 이 설정을 안 하면? Rx는 Tx가 보내는 신호를 보고도 "나한테 온 게 아니네"라며 무시.
    => 결과적으로 Rx에 신호가 들어오지 않으니 CSI 데이터도 아예 쌓이지 않게 됨.

      - esp_wifi_set_promiscuous(true): Promiscuous Mode 활성화
      - esp_wifi_set_promiscuous(false): Promiscuous Mode 비활성화
      - esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE): 채널 1번으로 고정 (WIFI_SECOND_CHAN_NONE은 HT40 모드에서 보조 채널이 없음을 의미)
    

    esp_wifi_set_channel() 함수를 사용하여 채널 1번으로 고정.
  */
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(11, WIFI_SECOND_CHAN_NONE);

  /*
    채널을 1번으로 고정 했으니 이제는 패킷을 쏘는 일만 남았는데, ESP-NOW로 패킷을 쏘기 전에 esp_wifi_set_promiscuous(false);를 호출하여 Promiscuous Mode를 꺼주는 것이 좋음.
    CSI 설정(esp_wifi_set_csi_config)이나 콜백 함수 등록 등을 '깨끗한 상태'에서 완료하기 위함.
    이렇게 해야만 ESP-NOW가 정상적으로 작동하면서 패킷이 802.11n 형식으로 전송되고, Rx 보드가 이를 인식하여 CSI 데이터를 뱉어냄.
    만약 이 설정을 안 하면? ESP-NOW 패킷이 제대로 전송되지 않거나, Rx 보드가 패킷을 인식하지 못해서 CSI 데이터가 아예 들어오지 않는 버그가 발생할 수 있음.

    이 과정들은 채널을 내 마음대로 바꾸기 위해 잠시 '수동 모드' 권한을 빌려왔다가, 채널만 바꾸고 다시 권한을 반납하는 과정으로 생각하면 됨.
  */
  esp_wifi_set_promiscuous(false);

  Serial.println("=================================");
  Serial.print("이 TX 보드의 MAC 주소: ");
  Serial.println(WiFi.macAddress()); // 디버깅용: 이 MAC 주소로 Rx 보드에서 필터링
  Serial.println("=================================");

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW Init Failed");
    return;
  }


  esp_now_peer_info_t peerInfo = {};

  /*
    브로드캐스트 주소로 설정하여, 이 패킷이 모든 수신자에게 전송되도록 함.
    이렇게 해야만 Rx 보드가 패킷을 인식하고 CSI 데이터를 뱉어냄.
      - broadcastAddress는 6바이트로 구성된 배열로, 모든 바이트가 0xFF로 설정되어 있음.
      - 이 주소는 Wi-Fi 네트워크에서 브로드캐스트 패킷을 보낼 때 사용되는 주소로, 네트워크에 연결된 모든 장치가 이 패킷을 수신하게 됨.

    추후에 Rx 보드에서 이 MAC 주소로 필터링하여, 우리 실험에 필요한 패킷만 골라낼 수 있을 듯함.
  */
  memcpy(peerInfo.peer_addr, broadcastAddress, 6);

  /*
    앞서 esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);에서 설정했던 채널 1번으로 고정.
    만약 송신기는 1번 채널에서 쏘는데 수첩에는 6번 채널이라고 적어두면 데이터가 전달되지 않음.
      - ESP-NOW는 Wi-Fi 채널과 밀접하게 연관되어 있기 때문에, 송신기와 수신기가 동일한 채널에 있어야만 통신이 가능함.
      - esp_wifi_set_channel() 함수를 사용하여 채널을 고정했지만, esp_now_add_peer() 함수에서도 해당 채널을 명시적으로 설정해주는 것이 좋음.
       이렇게 하면 ESP-NOW가 해당 채널에서 패킷을 전송하도록 보장할 수 있음.
  */
  peerInfo.channel = 11;
  /*
    암호화 설정: 암호화 없이 전송하도록 설정.
    암호화를 걸면 패킷 처리에 시간이 더 걸리고(오버헤드), Rx 측에서 CSI를 추출할 때 불필요한 계산 리소스가 소모될 수 있어 보통 false로 둠.
     - encrypt가 true로 설정되면, ESP-NOW는 데이터를 암호화하여 전송하며, 수신 측에서도 해당 데이터를 복호화해야 함.
     - encrypt가 false로 설정되면, 데이터는 암호화되지 않고 평문으로 전송됨.
  */
  peerInfo.encrypt = false;

  /*
    esp_now_add_peer(&peerInfo)
  
    Description:
      ESP-NOW peer 추가 함수. 송신기가 데이터를 전송할 대상 수신기를 등록하는 함수.
      peer_info 구조체에 설정된 MAC 주소, 채널, 암호화 정보 등을 바탕으로 peer를 추가함.
    
    Parameters:
      &peerInfo: ESP-NOW peer 정보를 담은 구조체의 포인터
        - peer_addr: 수신기의 MAC 주소 (6바이트 배열)
        - channel: 통신에 사용할 Wi-Fi 채널 번호 (송신기와 수신기가 동일해야 함)
        - encrypt: 암호화 여부 (true: 암호화 활성화, false: 평문 전송)
  
    Return Value:
      ESP_OK: peer 추가 성공
      ESP_ERR_ESPNOW_EXIST: 해당 peer가 이미 존재
      ESP_ERR_ESPNOW_FULL: peer 저장 공간 부족
      기타 에러 코드: 다양한 실패 원인
  
    Important Notes:
      - 브로드캐스트 주소(FF:FF:FF:FF:FF:FF)로 설정하면 모든 수신기가 패킷을 받게 됨.
      - peerInfo.channel은 esp_wifi_set_channel()에서 설정한 채널과 일치해야 함.
      - CSI 데이터 추출을 위해서는 송신기와 수신기가 반드시 같은 채널에 고정되어 있어야 함.
      - 이 함수 호출 이후 esp_wifi_config_espnow_rate()로 전송 속도를 802.11n(MCS0)으로 고정해야 Rx가 CSI 데이터를 정상 추출할 수 있음.
  */
  esp_now_add_peer(&peerInfo);

  // ESP-NOW 전송 속도를 802.11n(MCS0) 속도로 강제 고정
  // 이렇게 해야만 RX 보드가 패킷을 인식하고 CSI 데이터를 뱉어냅니다.
  /*
    esp_wifi_config_espnow_rate() 함수

    Description:
      ESP-NOW 전송 속도 설정 함수. 송신기가 패킷을 전송할 때 사용할 Wi-Fi PHY 속도를 설정하는 함수.
      이 설정은 송신기와 수신기 모두에서 일치해야만 통신이 원활하게 이루어짐.

    Parameters:
      ifx: 인터페이스 선택 (WIFI_IF_STA 또는 WIFI_IF_AP)
      rate: 설정할 PHY 속도 (예: WIFI_PHY_RATE_MCS0_SGI)

    Return Value:
      ESP_OK: 설정 성공
      기타 에러 코드: 다양한 실패 원인

    MCS0(Modulation and Coding Scheme 0)란?
    802.11n 규격에서 정의된 변조 및 코딩 방식 중 가장 낮은 속도(약 6.5Mbps)를 의미함.
    MCS0은 'BPSK'라는 아주 단순한 변조 방식을 사용하기 때문에, 신호가 약하거나 간섭이 심한 환경에서도 패킷이 끊기지 않고 안정적으로 전송될 수 있음.

    SGI(Short Guard Interval)란?
    전파를 쏠 때, 전파가 벽에 맞고 튕겨 나와서 서로 간섭하는 것을 막기 위해 패킷 사이에 아주 짧은 '휴식 시간'을 두는데, 이걸 Guard Interval(GI)이라고 함.
    SGI(Short Guard Interval)는 802.11n에서 도입된 기능으로, 패킷 간 간격을 기존 800ns에서 400ns로 줄여주는 기술임.
    따라서 CSI 수집 시 패킷을 아주 짧은 간격으로 촘촘하게 쏠 때 도움이 됨.

    참고.
    원래 ESP-NOW는 주변 환경에 따라 지가 알아서 속도를 조절(Auto-rate).
    하지만 CSI 실험에서는 어떤 패킷은 느리게 오고, 어떤 패킷은 빠르게 오면 전파 파형을 비교할 때 기준이 흔들림.
    그래서 이 함수를 써서 무조건 MCS0 속도에 Short GI 모드로만 전송하도록 강제로 고정시키는 것. 
    그래야 Rx 측에서 들어오는 CSI 데이터들이 일정한 기준 위에서 분석될 수 있음.
  */
  esp_wifi_config_espnow_rate(WIFI_IF_STA, WIFI_PHY_RATE_MCS0_SGI);

  Serial.println("TX Board Ready! Sending 802.11n(HT) packets for CSI...");
}

void loop() {
  // 1바이트짜리 가벼운 더미 데이터를 브로드캐스트 주소로 전송하여, Rx 보드가 패킷을 인식하고 CSI 데이터를 뱉어내도록 함.

  // dummy_data는 실제로 중요한 정보가 담긴 데이터가 아니라, 단지 패킷이 전송되고 있다는 신호 역할을 하는 더미 데이터임.
  uint8_t dummy_data = 1;
  // 패킷 전송: esp_now_send() 함수는 송신기가 데이터를 전송하는 함수로, 브로드캐스트 주소로 설정된 peer에게 dummy_data를 전송함.
  esp_now_send(broadcastAddress, &dummy_data, 1);
  
  delay(25); // 1초에 약 40번 전송
}