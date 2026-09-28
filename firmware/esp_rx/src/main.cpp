/*
  - 보드 레이트를 921600으로 올림.
  - 타 Wi-Fi를 고려하여 채널을 11로 이동.
*/

#include <WiFi.h>
#include <esp_wifi.h>

// TX 보드의 MAC 주소
const uint8_t TARGET_MAC[] = {0x68, 0xFE, 0x71, 0x21, 0x56, 0xE0};

typedef struct
{
    int8_t csi_data[256];
    int data_len;
    int rssi;
} csi_item_t;

QueueHandle_t csi_queue;

// 파이썬으로 보낼 패킷의 시퀀스 번호
uint16_t seq_num = 0;

void csi_callback(void *ctx, wifi_csi_info_t *info)
{
    uint8_t *mac = info->mac;

    bool is_target = true;
    for (int i = 0; i < 6; i++)
    {
        if (mac[i] != TARGET_MAC[i])
        {
            is_target = false;
            break;
        }
    }

    if (!is_target)
        return;

    csi_item_t item;
    item.data_len = info->len;
    item.rssi = info->rx_ctrl.rssi;

    if (item.data_len > 256)
        item.data_len = 256;

    memcpy(item.csi_data, info->buf, item.data_len);
    xQueueSendFromISR(csi_queue, &item, NULL);
}

void setup()
{
    Serial.begin(921600);

    csi_queue = xQueueCreate(20, sizeof(csi_item_t));

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(11, WIFI_SECOND_CHAN_NONE);

    wifi_csi_config_t csi_config = {
        .lltf_en = false,
        .htltf_en = true,
        .stbc_htltf2_en = true,
        .ltf_merge_en = false,
        .channel_filter_en = false,
        .manu_scale = false,
        .shift = false};

    esp_wifi_set_csi_config(&csi_config);
    esp_wifi_set_csi_rx_cb(&csi_callback, NULL);
    esp_wifi_set_csi(true);
}

void loop()
{
    csi_item_t item;

    if (xQueueReceive(csi_queue, &item, portMAX_DELAY))
    {

        // 파이썬 connector.py의 HEADER_FMT = "<BBHIbH" 에 맞춘 바이너리 버퍼 생성
        uint8_t buf[512];
        int idx = 0;

        buf[idx++] = 0xAA; // SYNC0
        buf[idx++] = 0xBB; // SYNC1

        uint32_t ts_us = micros();
        uint16_t length = item.data_len;
        int8_t rssi = item.rssi;

        memcpy(&buf[idx], &seq_num, 2);
        idx += 2;
        memcpy(&buf[idx], &ts_us, 4);
        idx += 4;
        buf[idx++] = (uint8_t)rssi;
        memcpy(&buf[idx], &length, 2);
        idx += 2;

        // CSI 페이로드 복사
        memcpy(&buf[idx], item.csi_data, length);
        idx += length;

        // 체크섬 계산 (파이썬 코드: offset 2 부터 HEADER_LEN+length-1 까지 XOR)
        uint8_t cs = 0;
        for (int i = 2; i < idx; i++)
        {
            cs ^= buf[i];
        }
        buf[idx++] = cs;

        // 완성된 이진 프레임 전송
        Serial.write(buf, idx);

        seq_num++;
    }
}