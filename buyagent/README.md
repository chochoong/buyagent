# 구매대행 모니터 v3 (확장형)

해외 쇼핑몰에서 **팔아도 되는 정품만 골라** 최저가를 찾고, 쿠팡과 스마트스토어 권장가를 계산합니다. 신상품, 재입고, 품절도 알려 줘요.

**상품, 카테고리, 쇼핑몰, 국가, 마켓을 코드 수정 없이 추가할 수 있게** 나눠 놓은 구조예요.

## 폴더 구조

```
buyagent/
├── main.py                 실행 파일
├── catalog/
│   ├── products.csv        ★ 감시할 상품 목록 (엑셀로 편집)
│   └── discover.csv        ★ 신상품 발견용 검색어
├── config/
│   ├── settings.toml       환율, 배대지 요금표, 국가별 면세 한도, 마켓 수수료
│   ├── settings.local.toml API 키·웹훅 (git 제외, .example 복사해서 만듦)
│   ├── categories.toml     카테고리 규칙, 차단 키워드, 경고 키워드
│   └── glossary.toml       한글 → 일본어 단어장 (규칙에 한글로 쓸 때)
├── sources/                쇼핑몰 플러그인 (파일 하나 = 쇼핑몰 하나)
│   ├── rakuten.py          라쿠텐 (일본)
│   ├── yahoo.py            Yahoo! 쇼핑 (일본)
│   ├── demo.py             예시 데이터 (--demo)
│   └── _template.py        새 쇼핑몰 만들 때 복사할 템플릿
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
```

Python 3.11 이상이 필요하고, 다른 패키지는 설치하지 않아도 됩니다.

### API 키 넣기

API 키와 웹훅 주소는 `config/settings.local.toml`에 넣어요. 이 파일은 git에 올라가지 않아요.

1. `config/settings.local.toml.example`을 복사해서 이름을 `settings.local.toml`로 바꿔요.
2. 키를 채워요. 여기 쓴 값이 `settings.toml`의 같은 항목을 덮어써요.

### 설정 점검 (`--check`)

상품을 추가하거나 설정을 바꾼 뒤에는 `python main.py --check`를 돌려 보세요. 실행 중에 조용히 틀리는 것들을 미리 잡아 줘요.

- **❌ 오류:** 상품 ID 중복, 국가 설정·배대지 요금표·환율 누락, 없는 배송 등급, 소스 이름 오타, 수수료를 퍼센트로 쓴 경우(0.109가 아니라 10.9) 등
- **⚠ 주의:** API 키 미입력, JAN 형식 이상, 상품명 조건(must_include·jan)이 없는 상품, 단어장에 없는 한글 등

오류가 있으면 종료 코드 1로 끝나요. 평소 실행할 때도 오류가 있으면 맨 위에 경고가 떠요.

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

**Windows (작업 스케줄러)** — 명령 프롬프트에서 한 번 실행:

```
schtasks /Create /SC HOURLY /TN buyagent /TR "cmd /c cd /d C:\workspace\buyagent\buyagent && python main.py >> run.log 2>&1"
```

**macOS / Linux (cron)**

```
0 * * * * cd /경로/buyagent && /usr/bin/python3 main.py >> run.log 2>&1
```

출력은 항상 UTF-8로 저장돼요 (이모지가 있어도 로그 파일로 넘길 때 멈추지 않음).

### 알림 규칙

- **항상 알림:** 품절, 재입고, 원가 변동 (`price_change_rate` 이상), 신상품
- **처음 한 번만 알림:** 판매처 확인, 가격 경쟁력, 관부가세, 마진 미달, 차단 같은 상태 경고. 금액만 조금 바뀐 같은 경고는 다시 보내지 않고, 경고가 사라졌다가 다시 생기면 다시 알려요.
- 메시지가 길면 Discord(1900자), Slack(3500자) 단위로 나눠서 보내요.

### API 호출 제한

`[sources.<이름>]`의 `min_interval`(호출 간격, 초)과 `retries`(429·서버 오류·네트워크 오류일 때 재시도 횟수)로 조절해요. 라쿠텐은 초당 1회 제한이 있어서 기본값이 1.1초예요.

## 주의

- 이 도구는 **판매 판단까지만** 해요. 구매, 결제, 마켓 등록은 사람이 확인한 뒤에 하세요.
- 쇼핑몰은 공식 API나 제휴 피드만 쓰세요. 크롤링은 약관 위반이 될 수 있어요.
- 카테고리 규칙은 법률 판단을 대신하지 않아요. 애매한 품목은 관세사에게 확인하세요.
