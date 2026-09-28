# Dataset

학습에 사용한 전체 CSI 원본 데이터(`collected_data.zip`, 약 76 MB)는 저장소 용량과 개인정보·실험환경 관리 문제를 고려해 이 제출본에서 제외했습니다.

재학습하려면 별도로 보관한 압축 파일을 해제하여 다음 구조로 배치합니다.

```text
software/ml_dl/data/collected_data/
├── empty/
├── p1_lying/
├── p1_moving/
└── p1_standing/
```

그 후 저장소 루트에서 다음 명령을 실행합니다.

```bash
python software/ml_dl/src/train_four_state_best_original.py \
  --input_dir software/ml_dl/data/collected_data \
  --output_dir software/ml_dl/models/four_state_best_original
```

학습 완료 모델과 검증 결과는 `software/ml_dl/models/four_state_best_original/`에 포함되어 있어 실시간 추론에는 원본 데이터가 필요하지 않습니다.
