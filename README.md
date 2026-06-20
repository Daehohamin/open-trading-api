# Samsung Electronics Auto Trader

이 프로젝트는 한국투자증권(KIS) Open API REST만을 사용하여 삼성전자(005930) 모의투자 자동매매를 수행하는 Python 시스템입니다. REST 기반으로 안전한 모의투자 주문, 계좌 조회, 보고서 생성을 지원합니다.

## 주요 개선 사항
- `get_price()`는 `tr_id: FHKST01010100`을 전송합니다.
- `get_balance()`는 `tr_id: VTTC8434R`과 `CTX_AREA_FK100` / `CTX_AREA_NK100`을 포함합니다.
- 모의현금 주문 TR ID는 공식 예제 기준 `VTTC0012U`(buy), `VTTC0011U`(sell)입니다.
- 계좌 조회는 `output1`에서 보유 종목을, `output2`에서 요약/현금을 파싱합니다.
- 주문 수량은 최소 1주, 최대 `MAX_ORDER_QUANTITY`로 제한됩니다.
- 삼성전자 보유 수량이 부족하면 매도 주문을 제출하지 않습니다.
- 거래 시간은 `Asia/Seoul` 기준 `09:10–15:30`으로 계산합니다.
- 실거래는 항상 비활성화되며 `--no-paper-trading` 사용은 차단됩니다.
- `--no-dry-run`은 `--confirm-paper-order`와 함께 사용해야 합니다.
- `--inspect`, `--show-orders`, `--report`, `--quantity`, `--buy-only`, `--sell-only` 옵션을 지원합니다.

## 실행 준비
### 필수 환경변수
- `GH_ACCOUNT`
- `GH_APPKEY`
- `GH_APPSECRET`

### 선택 환경변수
- `GH_PRODUCT_CODE` (기본값 `01`)

## 안전한 실행 명령
- 도움말 확인:
  - `python -m samsung_auto_trader.main --help`
- dry-run 단일 사이클:
  - `python -m samsung_auto_trader.main --once --dry-run --quantity 1`
- inspect 읽기 전용 모드:
  - `python -m samsung_auto_trader.main --inspect --show-orders --report`
- 모의투자 주문 제출(명시 확인 필요):
  - `python -m samsung_auto_trader.main --once --no-dry-run --confirm-paper-order --quantity 1`

## 옵션 설명
- `--once`: 한 사이클만 실행하고 종료
- `--dry-run`: 주문을 전송하지 않음
- `--no-dry-run`: 실제 주문 전송 허용 전 단계
- `--confirm-paper-order`: `--no-dry-run`과 함께 사용해야 함
- `--paper-trading`: 모의투자 TR ID 사용
- `--no-paper-trading`: 금지됨(실거래 비활성화 유지)
- `--offset`: 매수/매도 가격 오프셋
- `--quantity`: 주문 수량 (기본값 1)
- `--buy-only`: 매수만 실행
- `--sell-only`: 매도만 실행
- `--show-orders`: 최근 주문 내역 표시
- `--report`: 민감 정보를 제거한 보고서 생성
- `--inspect`: 읽기 전용 상태 점검

## 안전 설계
- 기본 모드: `dry_run` 및 `paper_trading` 활성화
- `--no-dry-run`은 `--confirm-paper-order`와 함께만 동작
- 계좌 현금은 `output2`에서 우선 추출하고 `dnca_tot_amt`/`prvs_rcdl_excc_amt`로 fallback
- 보유 내역은 `output1`에서 추출
- 주문 수량은 최소 1주, 최대 `MAX_ORDER_QUANTITY`
- 거래창은 `Asia/Seoul` 기준 `09:10–15:30`
- websocket 미사용, REST polling만 사용

## outputs
- `outputs/execution_report.md`: 실행 보고서
- `outputs/recent_orders.csv`: 최근 주문 기록
- `outputs/account_summary.svg`: 계좌 요약 시각화

## 검증 명령
- `python -m compileall samsung_auto_trader`
- `python -m unittest discover -s tests -v`
- `python -m samsung_auto_trader.main --help`
- `python -m samsung_auto_trader.main --inspect --show-orders --report`
- `python -m samsung_auto_trader.main --once --dry-run --quantity 1`

## 증빙
- `--inspect`는 주문을 전송하지 않고 계좌/주문 기록을 조회합니다.
- `--report`는 계좌 번호, App Key, App Secret, 토큰 등의 민감 정보를 포함하지 않습니다.
- `--once --dry-run --quantity 1`은 실제 주문을 제출하지 않고 동작을 검증합니다.
