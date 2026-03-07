SELECT DISTINCT
    rcept_no,
    viewer_url,
    corp_cls,
    corp_code,
    corp_name,
    report_nm,
    flr_nm,
    pblntf_ty,
    rcept_dt
FROM dart.dart_investment_events_copy_copy
WHERE TRIM(IFNULL(iscmp_cmpnm, '')) = '(대표자)'
