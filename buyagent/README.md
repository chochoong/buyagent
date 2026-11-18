# 구매대행 모니터 v3 (확장형)

해외 쇼핑몰에서 **팔아도 되는 정품만 골라** 최저가를 찾고, 쿠팡과 스마트스토어 권장가를 계산합니다. 신상품, 재입고, 품절도 알려 줘요.

**상품, 카테고리, 쇼핑몰, 국가, 마켓을 코드 수정 없이 추가할 수 있게** 나눠 놓은 구조예요.

## 폴더 구조

```
buyagent/
├── main.py                 실행 파일 (구매대행 감시)
├── sourcing.py             사입 원가 계산기 (중국 1688 등)
├── research.py             상품 발굴: 네이버 검색 추이 (NAVER API HUB)
├── run.bat                 1회 실행 + run.log 기록 (작업 스케줄러용)
├── schedule.bat            작업 스케줄러에 매시간 실행 등록
├── catalog/
│   ├── products.csv        ★ 감시할 상품 목록 (엑셀로 편집)
│   ├── discover.csv        ★ 신상품 발견용 검색어
│   ├── manual_prices.csv   ★ API 없는 쇼핑몰(타오바오·알리) 가격 직접 입력
│   └── sourcing.csv        ★ 사입 계산할 상품 (1688 단가·수량·무게)
├── config/
│   ├── settings.toml       환율, 배대지 요금표, 국가별 면세 한도, 마켓 수수료
│   ├── settings.local.toml API 키·웹훅 (git 제외, .example 복사해서 만듦)
│   ├── categories.toml     카테고리 규칙, 차단 키워드, 경고 키워드
│   └── glossary.toml       한글 → 일본어 단어장 (규칙에 한글로 쓸 때)
├── sources/                쇼핑몰 플러그인 (파일 하나 = 쇼핑몰 하나)
│   ├── rakuten.py          라쿠텐 (일본, API)
│   ├── rakuten_web.py      라쿠텐 (일본, API 키 없이 검색 페이지 수집 · 크롬 필요)
│   ├── yahoo.py            Yahoo! 쇼핑 (일본)
│   ├── manual.py           수동 가격 (catalog/manual_prices.csv)
│   ├── demo.py             예시 데이터 (--demo)
│   └── _template.py        새 쇼핑몰 만들 때 복사할 템플릿
├── tests/                  회귀 테스트 (python -m unittest)
└── core/                   공통 로직 (평소엔 건드릴 일 없음)
    ├── rules.py            판매 가능 여부 판단
    ├── pricing.py          원가·관부가세·권장가 계산
    ├── fx.py               다통화 환율
    ├── storage.py          가격 이력·신상 기록 (SQLite)
    └── report.py           콘솔·CSV·웹훅 출력
```

## 실행

```bash
python main.py --demo            # 키 없이 예시 데이터로 실행
python main.py --check           # 설정과 상품 목록 점검 (차단 상품 미리 확인)
python main.py                   # 실제 실행
python main.py --bundle 5        # 5개를 한 번에 배송받는 기준으로 개당 원가 계산
python main.py --only chiikawa   # ID에 'chiikawa'가 들어간 것만 실행
python main.py --probe           # API 키 확인: 쇼핑몰마다 검색 1번 (--probe "검색어")
python main.py --test-alert      # 웹훅으로 테스트 메시지 보내기
python main.py --search "セイコー SBTQ041" --category watch   # 직접 검색: 등록 없이 최저가·원가·권장가 (→ reports/search.json, 대시보드 '직접 검색')
python main.py --search "Seiko SBTQ041" --category watch --market aliexpress   # 알리에서 직접 검색 (--market 기본값 rakuten)
python main.py --no-alert        # 시험 실행: 웹훅 알림만 안 보냄 (조회·history.db·리포트·latest.json 은 평소대로)
python -m unittest               # 코드 고친 뒤 기존 기능이 그대로인지 확인
```

Python 3.11 이상이 필요하고, 다른 패키지는 설치하지 않아도 됩니다.

### 자체 화면 (`web.py`)

```bash
python web.py        # 또는 web.bat 더블클릭 → http://127.0.0.1:8766 (내 PC에서만 열림)
```

trade_hub 없이도 buyagent 혼자 쓰는 작업 화면이에요. 계산은 모두 `main.py`가 하고, 화면은 실행하고 결과 파일을 보여 줄 뿐이에요.

- **직접 검색:** 검색어 + 카테고리 → 최저가·원가·권장가 (원가 낮은 순). 감시·알림에는 영향 없음
- **사이트 선택(`--market`):** `rakuten`(기본, 한글은 일본어로 번역) / `aliexpress`(한글은 영어로 번역). 알리는 공식 제휴 API라 키 3개(`ALIEXPRESS_APP_KEY`·`ALIEXPRESS_APP_SECRET`·`ALIEXPRESS_TRACKING_ID`)를 `buyagent/.env`에 넣어야 동작해요. 키가 없으면 "키 없음" 실패로 기록돼요. 알리는 한국 직배송 기준(`CN_DIRECT`, 배대지 요금 0)으로 원가를 계산해요. 타오바오·1688은 공식 API를 개인이 쓸 수 없고 봇 차단 우회가 필요해서 넣지 않아요(수동 가격으로 입력).
- **이 검색을 감시에 추가:** 방금 검색한 조건(검색어·필수 단어·제외 단어)을 `products.csv`에 한 줄 추가 → 다음 감시부터 매시간 추적
- **감시 켜기/끄기**, **감시 시험 실행**(`--no-alert`: 웹훅만 안 보냄)
- 최근 감시 결과, 감시 상품, 수동 가격 보기

상품이 들어오는 길은 두 가지예요. 둘 다 같은 `products.csv`에 쌓이고 같은 방식으로 감시돼요.

1. **파이프라인:** 시장조사·아이템 발굴(1번) → 공급처 발굴(2번) → `export-buyagent` → buyagent 감시 (메모에 "2번 공급처 발굴에서 가져옴")
2. **직접 검색:** 이미 정한 상품(예: 세이코 SBTQ041)은 1·2번을 건너뛰고 화면에서 검색 → 감시에 추가 (메모에 "직접 검색에서 추가")

trade_hub 대시보드는 같은 결과 파일(`reports/latest.json`, `reports/search.json`)을 읽기만 해서, 두 화면을 같이 켜 둬도 돼요.

### API 키 넣기

API 키와 웹훅 주소는 `config/settings.local.toml`에 넣어요. 이 파일은 git에 올라가지 않아요.

1. `config/settings.local.toml.example`을 복사해서 이름을 `settings.local.toml`로 바꿔요.
2. 키를 채워요. 여기 쓴 값이 `settings.toml`의 같은 항목을 덮어써요.
3. `python main.py --probe`로 확인해요. 쇼핑몰마다 검색을 1번 해서 응답이 오는지, 가격·재고·JAN이 제대로 읽히는지 보여줘요. 거절되면 쇼핑몰이 보낸 사유가 그대로 나와요.
4. 알림을 쓰려면 `[alerts] webhook_url`을 넣고 `python main.py --test-alert`로 메시지가 오는지 확인해요.

- **Yahoo! 쇼핑:** https://e.developer.yahoo.co.jp/register 에서 앱을 등록하면 나오는 Client ID를 `app_id`에 넣어요.
- **라쿠텐:** https://webservice.rakuten.co.jp/ 에서 앱을 등록하면 나오는 `applicationId`를 넣어요. access key가 함께 나오면 `access_key`에도 넣고, 없으면 비워 둔 채 `--probe`로 확인해요.
- **라쿠텐 키 없이:** `[sources.rakuten_web]`이 PC의 크롬을 화면 없이 띄워 검색 페이지를 읽어요. 키는 필요 없지만 정렬이 안 돼서(robots.txt가 금지) 가격순·최신순 검색을 못 하고, 그래서 신상 발견에는 쓰이지 않아요. 검색 1건에 몇 초 걸려요. 라쿠텐 API 키를 받으면 `[sources.rakuten_web] enabled = false`로 끄세요 (둘 다 켜면 같은 상품을 두 번 검색해요).
- **Discord 웹훅:** 채널 설정 → 연동 → 웹후크 → 새 웹후크 → URL 복사.

### 설정 점검 (`--check`)

상품을 추가하거나 설정을 바꾼 뒤에는 `python main.py --check`를 돌려 보세요. 실행 중에 조용히 틀리는 것들을 미리 잡아 줘요.

- **❌ 오류:** 상품 ID 중복, 국가 설정·배대지 요금표·환율 누락, 없는 배송 등급, 소스 이름 오타, 수수료를 퍼센트로 쓴 경우(0.109가 아니라 10.9) 등
- **⚠ 주의:** API 키 미입력, JAN 형식 이상, 상품명 조건(must_include·jan)이 없는 상품, 단어장에 없는 한글 등

오류가 있으면 종료 코드 1로 끝나요. 평소 실행할 때도 오류가 있으면 맨 위에 경고가 떠요.

## 중국 소싱

### 사입 계산기 (`python sourcing.py`)

1688 등에서 여러 개를 한 번에 들여와 재고로 파는 경우의 원가를 계산해요. `catalog/sourcing.csv`에 한 줄씩 넣어요.

| 열 | 내용 |
|---|---|
| `unit_price`, `currency` | 1688 단가 (기본 CNY) |
| `qty` | 수량 |
| `cn_shipping` | 판매자 → 중국 배대지 배송비 (이 줄 전체) |
| `weight_kg`, `size_cm` | **개당** 포장 무게, 포장 크기 (`33x13x5`) |
| `batch` | 같은 값끼리 한 번에 발송 → 최소 배송비·통관 수수료를 나눠 냄 |
| `cert_cost_krw` | KC 인증 비용 (인증 받은 키보드 등). 수량으로 나눠 원가에 포함 |
| `price_<마켓>` | 쿠팡·스마트스토어 경쟁 판매가 → 그 가격에 팔 때 마진 확인 |

```bash
python sourcing.py              # 해운 기준
python sourcing.py --air        # 항공 기준
python sourcing.py --offline    # 환율 자동 조회 없이
```

- **계산 기준:** 구매대행 수수료, 국제 배송비(무게·부피 중 큰 쪽), 관세, 통관 수수료, 포장·택배비를 넣어서 **개당 원가, 마켓별 권장가와 마진, 총 투입금**을 계산해요.
- **요금 설정:** `config/sourcing.toml`에 있어요. **예시값이니 실제 견적으로 바꿔 주세요.**
- **부가세:** 일반과세자는 수입 부가세를 환급받아서 원가에서 빠지고, 판매가의 1/11은 납부하는 것으로 계산해요. 간이과세자는 `[tax] type = "simple"`으로 바꿔요.
- **사입 금지:** `categories.toml`에서 `bulk_ok = false`인 카테고리(키보드 본체 등 KC 대상)는 ⛔로 표시돼요.

### 상품 발굴: 네이버 검색 추이 (`python research.py`)

```bash
python research.py 키캡 손목받침 장패드 기계식키보드
```

키워드마다 최근 12개월 월별 검색량 그래프, 가장 많이 찾는 달, 최근 3개월 증감을 보여줘요. 한 번에 5개씩 묶어서 조회해서, 같은 묶음 안에서는 어느 키워드가 더 많이 검색되는지도 비교할 수 있어요.

- **키 발급:** 네이버 클라우드 플랫폼(https://www.ncloud.com) 가입 → 콘솔 → **NAVER API HUB** → Application 등록에서 **검색어 트렌드**를 골라요. 발급된 Client ID와 Client Secret을 `settings.local.toml`의 `[naver]`에 넣어요.
- **가격은 못 가져와요:** 네이버 쇼핑 검색 API(상품·가격)는 **2026년 7월 31일에 종료**됐고 대체 API가 없어요. 국내 판매가는 `sourcing.csv`의 `price_coupang`, `price_naver`에 직접 적어 주세요.

### API 없는 쇼핑몰 가격 직접 입력 (키보드 구매대행 등)

타오바오·알리처럼 API가 없는 곳은 `catalog/manual_prices.csv`에 가격을 직접 적어요.

1. `products.csv`에 상품을 추가하고, `sources` 열에 `manual`을 써요.
2. `manual_prices.csv`에 같은 `id`로 가격, 통화(CNY), 판매처, 확인 날짜(`checked`)를 적어요.
3. `shipping`(판매자 → 배대지 현지 배송비)은 확인한 값을 적어요. **무료배송이면 `0`을 꼭 적으세요.** 빈 칸은 "미확인"으로 보고 0으로 가정해 계산한 뒤 "현지 배송비 미확인 → 확인 후 shipping 입력" 알림을 보내요.

그러면 원가·권장가 계산, 가격이 바뀔 때 알림, 품절(`in_stock = N`) 알림이 다른 쇼핑몰과 똑같이 돼요. `checked` 날짜가 7일 넘게 지나면 "가격 다시 확인" 알림이 와요.

## 확장 방법

### 1. 상품 추가: `catalog/products.csv`에 한 줄 추가

엑셀로 열어서 편집한 뒤 **"CSV UTF-8"** 형식으로 저장하세요.

| 열 | 설명 | 예시 |
|---|---|---|
| id | 영문 고유 ID | `hobonichi-2027-a6` |
| category | categories.toml의 키 | `stationery` |
| keyword | 현지 검색어 | `ほぼ日手帳 2027 A6` |
| jan | 바코드 번호. 넣으면 JAN이 일치하는 상품만 인정 (권장) | `4580...` |
| must_include | 상품명에 반드시 있어야 하는 단어. 여러 개는 `\|`로 구분 | `ほぼ日手帳\|2027\|A6` |
| exclude | 있으면 제외할 단어 | `カバー付き` |
| trusted_shops | 이 상점에서 산 것만 인정 (비우면 모든 상점) | |
| sources | 특정 쇼핑몰만 검색 (비우면 전체) | `rakuten\|yahoo` |
| shipping_profile / bundle_size | 카테고리 기본값을 이 상품만 다르게 | `large` / `3` |
| my_price_coupang / my_price_naver | 현재 내 판매가 (마진 점검용) | `29900` |
| hub_product_id | trade_hub 품목 번호 (대시보드 연결용, 비워도 됨). 정수가 아니면 `--check`에서 경고 | `23` |
| active | `N`이면 건너뜀 | `Y` |

### 2. 새 품목 종류 추가: `config/categories.toml`

```toml
[categories.tea_ware]
name = "다기·찻잔"
allowed = true
duty_rate = 0.08
shipping_profile = "large"
min_margin_krw = 8000
notice = "본 상품은 해외구매대행 상품입니다."
```

팔면 안 되는 종류는 `allowed = false`로 두고, `[[block_rules]]`에 차단 키워드를 넣으면 돼요. 정상 상품이 잘못 걸리면 그 규칙에 `except = ["クリップ"]`처럼 예외 단어를 넣으세요. 예외 단어를 먼저 지운 뒤 검사해요 (예: `クリップ`가 화장품 키워드 `リップ`에 걸리지 않게). 확인만 필요한 사항은 `[[warn_rules]]`에 넣으면 차단하지 않고 알림만 보내요.

**한글로 써도 돼요.** `keywords`와 `except`에 한글을 쓰면 `config/glossary.toml` 단어장에서 일본어로 바꿔 줘요.

```toml
except = ["특전포함"]          # → ["特典付き", "特典付", "特典あり"] 로 적용
```

단어장에 없는 한글은 실행할 때 경고가 나와요. 그럴 땐 `glossary.toml`의 `[words]`에 `"한글" = ["日本語", ...]` 한 줄을 추가하세요. 적용 결과는 `python main.py --check`로 확인할 수 있어요.

미리 넣어 둔 카테고리는 이래요.
- **판매 가능:** 시계, 아크릴·캐릭터 잡화, 피규어 키링, 문구·다이어리, 의류, 가방, 주방·생활용품
- **등록 금지:** 봉제인형, 유아용품, 식품, 건강기능식품, 화장품, 전자제품, 한정 증정품, 도검

### 3. 새 쇼핑몰이나 국가 추가: `sources/` 폴더

1. `sources/_template.py`를 복사해서 이름을 `sources/<이름>.py`로 바꾸고, API를 호출하는 `search()`를 채워요.
2. `settings.toml`에 `[sources.<이름>]` 섹션과 API 키를 추가해요.
3. 국가가 처음이면 다음 세 가지도 `settings.toml`에 추가해요.
   - `[origins.<국가>]` 면세 한도
   - `[shipping_profiles.<국가>]` 배대지 요금표
   - `[fx.manual_krw]` 통화

필터, 계산, 알림은 공통 코드가 처리하기 때문에 **쇼핑몰 파일은 검색 결과를 `Offer` 형식으로 바꿔 주기만** 하면 돼요. `python main.py --check`로 연결됐는지 확인하세요.

### 4. 새 판매 마켓 추가 (예: 11번가)

1. `settings.toml`에 `[markets.st11]` 섹션을 만들고 수수료를 넣어요.
2. `products.csv`에 `my_price_st11` 열을 추가해요.

## 계산 기준

```
개당 원가 = 현지가 × 환율 × (1 + 카드수수료)
          + (배대지 요금 + 국내배송비) ÷ 묶음수량
          + 포장비
          + [1회 배송 물품가가 면세 한도 초과 시] 예상 관부가세 ÷ 묶음수량
```

- 묶음 배송을 하면 1회 배송 물품가가 합산돼요. 그래서 면세 한도(일본 $150, 미국 $200)를 넘으면 관부가세 경고가 떠요.
- 마진에는 사업자 부가세, 소득세, 광고비가 들어 있지 않아요.

## 자동 실행 (매시간)

**Windows:** `schedule.bat`을 더블클릭하면 작업 스케줄러에 등록돼요. 매시간 `run.bat`이 실행되고, 결과는 `run.log`에 쌓여요.

- `run.bat`: 한 번 실행하고 출력을 `run.log`에 덧붙여요. 2MB가 넘으면 `run.old.log`로 옮겨요. 더블클릭해서 지금 바로 한 번 돌릴 수도 있어요.
- 등록 해제: `schtasks /Delete /TN buyagent /F`
- 등록 확인: `schtasks /Query /TN buyagent`

**macOS / Linux (cron)**

```
0 * * * * cd /경로/buyagent && /usr/bin/python3 main.py >> run.log 2>&1
```

출력은 항상 UTF-8로 저장돼요 (이모지가 있어도 로그 파일로 넘길 때 멈추지 않음).

### 실행 실패 알림

혼자 돌아가는 프로그램이 조용히 멈추지 않도록, 아래 경우엔 웹훅으로 **🚨 모니터 실행 실패** 알림이 가요.

- 프로그램이 죽었을 때 (설정 파일 문법 오류 등). 설정을 못 읽어도 웹훅 주소만 따로 찾아서 보내요.
- 쓸 수 있는 쇼핑몰이 없을 때 (키 미입력)
- 감시 대상 **전부** 조회에 실패했을 때 (API 키 만료, 인터넷 끊김 등)
- `--check` 기준 설정 오류가 있을 때

실행 실패면 종료 코드 1, 정상이면 0으로 끝나요 (일부 상품이 차단·품절이어도 정상). `--no-alert`·`--demo` 실행은 이 알림을 보내지 않고 `health.json`도 바꾸지 않아요.

같은 문제가 계속되면 매시간 보내지 않고 **하루에 한 번만** 다시 알려요. 문제가 해결되면 **✅ 정상 복구** 알림이 1번 가요. 상태는 `health.json`에 저장돼요.

### 최신 결과 JSON (`reports/latest.json`)

실행이 끝날 때마다(실패해도) `reports/latest.json`을 덮어써요. trade_hub 대시보드가 읽는 파일이라 **키 이름을 바꾸면 안 돼요.** 임시 파일에 다 쓴 뒤 이름을 바꾸므로 읽는 쪽이 반쯤 쓰인 파일을 보지 않아요. `report_*.csv`도 그대로 남아요.

- 맨 위: `ok`, `error`(없으면 null), `generated_at`, `demo`, `markets`(예: `coupang`, `naver`), `items`
- `items` 한 줄: `kind`(감시/신상), `id`, `hub_product_id`, `name`, `category`, `status`(`ok`/`blocked`/`no_offer`/`error`), `blocked_reason`, `best_offer`(source·shop·url·price·shipping·shipping_known·currency·in_stock, 배송비 미확인이면 shipping은 null), `cost_krw`, `quotes`(market·recommended_price·margin·my_price·my_margin), `warnings`(목록), `events`(목록), `notice`

### 알림 규칙

- **항상 알림:** 품절, 재입고, 원가 변동 (`price_change_rate` 이상), 신상품
- **처음 한 번만 알림:** 판매처 확인, 가격 경쟁력, 관부가세, 마진 미달, 차단 같은 상태 경고. 금액만 조금 바뀐 같은 경고는 다시 보내지 않고, 경고가 사라졌다가 다시 생기면 다시 알려요.
- 메시지가 길면 Discord(1900자), Slack(3500자) 단위로 나눠서 보내요.

### 신상 발견과 쇼핑몰

신상 발견은 최신순 정렬이 필요해서 **라쿠텐에서만** 검색해요. Yahoo! 쇼핑 API에는 최신순 정렬이 없어서(추천순·가격순·리뷰순만 지원) 신상 발견에서는 자동으로 빠져요. 고정 상품 최저가 감시에는 두 곳 모두 쓰여요.

### API 호출 제한

`[sources.<이름>]`의 `min_interval`(호출 간격, 초)과 `retries`(429·서버 오류·네트워크 오류일 때 재시도 횟수)로 조절해요. 라쿠텐은 초당 1회 제한이 있어서 기본값이 1.1초예요.

## 주의

- 이 도구는 **판매 판단까지만** 해요. 구매, 결제, 마켓 등록은 사람이 확인한 뒤에 하세요.
- 쇼핑몰은 공식 API나 제휴 피드만 쓰세요. 크롤링은 약관 위반이 될 수 있어요.
- 카테고리 규칙은 법률 판단을 대신하지 않아요. 애매한 품목은 관세사에게 확인하세요.
