<?php

namespace Integro\UnitLinked;

define('MIN_SURRENDER_VALUE', 100.00);

class SurrenderValue
{
    public function penaltyRate(Policy $policy): float
    {
        return match (true) {
            $policy->yearsInForce < 3 => 0.05,
            $policy->yearsInForce < 5 => 0.03,
            default => 0.0,
        };
    }

    public function calculate(Policy $policy): float
    {
        if ($policy->status === 'L') {
            return 0.0;
        }
        $penaltyAmt = round($policy->fundValue * $this->penaltyRate($policy), 2);
        $surrenderValue = $policy->fundValue - $penaltyAmt;
        if ($surrenderValue < MIN_SURRENDER_VALUE && !in_array($policy->status, ['P', 'R'], true))
            $surrenderValue = MIN_SURRENDER_VALUE;
        return $surrenderValue;
    }
}
