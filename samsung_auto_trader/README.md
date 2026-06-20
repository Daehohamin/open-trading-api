# Samsung Auto Trader

이 문서는 `samsung_auto_trader` 패키지의 설치 및 안전한 실행 방법을 설명합니다.

## 패키지 개요
- `samsung_auto_trader.api_client`는 KIS REST API 호출을 처리합니다.
- `samsung_auto_trader.auth`는 OAuth 토큰 발급과 동일일 토큰 캐싱을 관리합니다.
- `samsung_auto_trader.market_data`는 현재가 조회를 수행합니다.
- `samsung_auto_trader.account`는 잔고와 보유종목을 조회합니다.
- `samsung_auto_trader.orders`는 모의 현금 주문을 실행합니다.
- `samsung_auto_trader.trader`는 CLI와 안전 검증, 보고서 생성을 담당합니다.

## 안전 강화 사항
- REST만 사용하며 websocket을 도입하지 않습니다.
- `get_price()`에 공식 TR ID `FHKST01010100`을 사용합니다.
- `get_balance()`에 공식 모의 TR ID `VTTC8434R`과 `CTX_AREA_FK100` / `CTX_AREA_NK100`을 포함합니다.
- 매수 트랜잭션은 `VTTC0012U`, 매도 트랜잭션은 `VTTC0011U` 모의 TR ID를 사용합니다.
- `--no-dry-run`은 `--confirm-paper-order`와 함께만 허용됩니다.
- 실제 매매는 항상 비활성화되며 paper trading 전용입니다.
- `--inspect`는 읽기 전용입니다.

## 실행 예시
- 도움말:
  - `python -m samsung_auto_trader.main --help`
- dry-run 테스트:
  - `python -m samsung_auto_trader.main --once --dry-run --quantity 1`
- inspect read-only 상태 확인:
  - `python -m samsung_auto_trader.main --inspect --show-orders --report`
- 모의투자 주문 제출(명시 확인 필요):
  - `python -m samsung_auto_trader.main --once --no-dry-run --confirm-paper-order --quantity 1`

## 보고서 생성
- `--report`는 `outputs/execution_report.md`, `outputs/recent_orders.csv`, `outputs/account_summary.svg`를 생성합니다.
- 생성된 파일에는 계좌 번호, App Key, App Secret, 토큰 등 민감 정보가 포함되지 않습니다.

## 검증
- `python -m compileall samsung_auto_trader`
- `python -m unittest discover -s tests -v`
