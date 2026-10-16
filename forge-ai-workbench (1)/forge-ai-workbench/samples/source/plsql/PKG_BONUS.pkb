CREATE OR REPLACE PACKAGE BODY pkg_bonus AS
  c_terminal_rate  CONSTANT NUMBER := 0.015;
  c_loyalty_years  CONSTANT NUMBER := 10;

  FUNCTION terminal_bonus(p_policy_id IN policies.policy_id%TYPE) RETURN NUMBER IS
    v_fund   policies.fund_value%TYPE;
    v_years  policies.years_in_force%TYPE;
    v_rate   NUMBER;
  BEGIN
    SELECT fund_value, years_in_force INTO v_fund, v_years FROM policies WHERE policy_id = p_policy_id;
    IF v_years >= c_loyalty_years THEN
      v_rate := c_terminal_rate + 0.005;
    ELSE
      v_rate := c_terminal_rate;
    END IF;
    RETURN ROUND(v_fund * v_rate, 2);
  END terminal_bonus;
END pkg_bonus;
/
