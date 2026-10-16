-- Integro - Level Term Assurance schema (PostgreSQL)
CREATE TABLE integro.lta_policy (
    policy_ref         VARCHAR(10)    PRIMARY KEY,
    status_code        CHAR(1)        NOT NULL CHECK (status_code IN ('A','L','P','R')),
    is_smoker          BOOLEAN        NOT NULL,
    payment_frequency  CHAR(1)        NOT NULL CHECK (payment_frequency IN ('A','M','Q')),
    holder_age         SMALLINT       NOT NULL,
    term_years         SMALLINT       NOT NULL,
    sum_assured        DECIMAL(11,2)  NOT NULL,
    base_rate          DECIMAL(5,4)   NOT NULL,
    fund_value         DECIMAL(11,2),
    years_in_force     SMALLINT       NOT NULL,
    monthly_premium    DECIMAL(9,2)   NOT NULL,
    party_id           VARCHAR(10)    NOT NULL,
    commencement_date  DATE           NOT NULL
);

CREATE TABLE integro.party (
    party_id        VARCHAR(10)   PRIMARY KEY,
    full_name       VARCHAR(120)  NOT NULL,
    party_birth_dt  DATE          NOT NULL,
    post_code       VARCHAR(8),
    ni_number       VARCHAR(20)
);
