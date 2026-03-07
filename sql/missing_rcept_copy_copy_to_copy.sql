SELECT f.*
FROM dart.dart_investment_events_copy_copy f
LEFT JOIN dart.dart_investment_events_copy d
  ON d.rcept_no = f.rcept_no
WHERE d.rcept_no IS NULL
