import pandas as pd
import numpy as np
import random

def make_sample_transfer_data_1y(seed: int = 20260217) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    random.seed(seed)

    corp_pool = [
        ("K", "00267906", "베뉴지"),
        ("K", "00593000", "한화솔루션"),
        ("Y", "00126380", "에코프로"),
        ("N", "00401731", "카카오"),
        ("K", "00164779", "SK네트웍스"),
        ("Y", "00764742", "두산퓨얼셀"),
    ]

    target_pool = [
        "삼성전자주식회사", "포스코퓨처엠", "포스코홀딩스", "포스코인터내셔널", "에코프로비엠",
        "LG에너지솔루션", "현대차", "기아", "네이버", "카카오", "셀트리온", "SK하이닉스", "한화에어로스페이스"
    ]

    reason_pool = [
        "자산매각을 통한 자금유동성 확보로 신규투자하여 수익성 창출",
        "비핵심자산 정리 및 재무구조 개선",
        "투자 포트폴리오 조정 및 현금흐름 안정화",
    ]

    # 최근 1년(월 단위) - 2025-03 ~ 2026-02
    months = pd.date_range("2025-03-01", "2026-02-01", freq="MS")

    rows = []

    for m in months:
        # 월별 공시 1~2건
        n_filings = int(rng.integers(1, 3))

        for _ in range(n_filings):
            filing_date = (m + pd.Timedelta(days=int(rng.integers(0, 27)))).date()
            corp_cls, corp_code, corp_name = random.choice(corp_pool)

            # rcept_no: YYYYMMDD + 6자리 일련번호
            seq = int(rng.integers(100000, 999999))
            rcept_no = f"{filing_date.strftime('%Y%m%d')}{seq:06d}"

            # 1) 실제 처분 라인 (음수)
            sold_cmp = random.choice(target_pool)
            sold_shares = -int(rng.integers(10_000, 800_000))
            sold_unit_price = int(rng.integers(20_000, 220_000))
            sold_amount = int(sold_shares * sold_unit_price)

            rows.append({
                "rcept_no": rcept_no,
                "rcept_dt": filing_date.isoformat(),
                "corp_cls": corp_cls,
                "corp_code": corp_code,
                "corp_name": corp_name,
                "report_nm": "타법인주식및출자증권처분결정",
                "flr_nm": corp_name,
                "pblntf_ty": "I",
                "source": "INIT+DOC",
                "iscmp_cmpnm": sold_cmp,
                "trfdtl_stkcnt": sold_shares,
                "trfdtl_trfprc": sold_amount,
                "trfdtl_tast": round(float(rng.uniform(0.5, 15.0)), 2),
                "trfdtl_ecpt": int(rng.integers(3_000_000_000, 600_000_000_000)),
                "attrf_owstkcnt": int(rng.integers(10_000, 2_500_000)),
                "attrf_eqrt": round(float(rng.uniform(0.0005, 0.08)), 3),
                "trf_pp": random.choice(reason_pool),
                "trf_prd": filing_date.isoformat(),
                "dlptn_cmpnm": pd.NA,
                "bddd": filing_date.isoformat(),
                "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
            })

            # 2) 처분대금 재투자(취득계획) 라인 2~5개
            n_plan = int(rng.integers(2, 6))
            plan_targets = random.sample(target_pool, k=n_plan)

            for t in plan_targets:
                shares = int(rng.integers(5_000, 80_000))
                unit_price = int(rng.integers(20_000, 220_000))
                amt = int(shares * unit_price)

                rows.append({
                    "rcept_no": rcept_no,
                    "rcept_dt": filing_date.isoformat(),
                    "corp_cls": corp_cls,
                    "corp_code": corp_code,
                    "corp_name": corp_name,
                    "report_nm": "타법인주식및출자증권처분결정",
                    "flr_nm": corp_name,
                    "pblntf_ty": pd.NA,
                    "source": "INIT+DOC+NOTE_PLAN",
                    "iscmp_cmpnm": t,
                    "trfdtl_stkcnt": shares,
                    "trfdtl_trfprc": amt,
                    "trfdtl_tast": pd.NA,
                    "trfdtl_ecpt": pd.NA,
                    "attrf_owstkcnt": pd.NA,
                    "attrf_eqrt": pd.NA,
                    "trf_pp": "처분대금 재투자(취득계획)",
                    "trf_prd": filing_date.isoformat(),
                    "dlptn_cmpnm": pd.NA,
                    "bddd": filing_date.isoformat(),
                    "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                })

    cols = [
        "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name", "report_nm",
        "flr_nm", "pblntf_ty", "source", "iscmp_cmpnm", "trfdtl_stkcnt",
        "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt", "attrf_owstkcnt",
        "attrf_eqrt", "trf_pp", "trf_prd", "dlptn_cmpnm", "bddd", "viewer_url"
    ]

    df = (
        pd.DataFrame(rows)[cols]
        .sort_values(["rcept_dt", "rcept_no"])
        .reset_index(drop=True)
    )
    return df


# 사용 예시
df_sample_1y = make_sample_transfer_data_1y(seed=20260217)
print("shape:", df_sample_1y.shape)
print(df_sample_1y.head(10))

# 필요하면 CSV 저장
df_sample_1y.to_csv("sample_transfer_1y.csv", index=False, encoding="utf-8-sig")
