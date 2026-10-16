<?php
declare(strict_types=1);

namespace Integro\Lta;

/**
 * Level Term Assurance premium calculation (Integro).
 */
final class PremiumCalculator
{
    public const ADMIN_FEE = 30.00;
    public const SMOKER_LOADING = 1.5;
    public const AGE_LOAD_THRESHOLD = 60;
    public const AGE_LOAD_FACTOR = 1.25;
    private const ANNUAL_DISC_RATE = 0.03;

    private float $roundingPrecision = 2;

    public function riskFactor(Policy $policy): float
    {
        $riskFactor = 1.0;
        if ($policy->isSmoker()) {
            $riskFactor *= self::SMOKER_LOADING;
        }
        // NB: strictly greater than - see INT-2231
        if ($policy->holderAge > self::AGE_LOAD_THRESHOLD) {
            $riskFactor = round($riskFactor * self::AGE_LOAD_FACTOR, 4);
        }
        return $riskFactor;
    }

    public function termFactor(int $termYears): float
    {
        if ($termYears <= 10) {
            $termFactor = 1.000;
        } elseif ($termYears <= 20) {
            $termFactor = 1.150;
        } else {
            $termFactor = 1.300;
        }
        return $termFactor;
    }

    public function monthlyPremium(Policy $policy): float
    {
        $basePremium = ($policy->sumAssured / 1000) * $policy->baseRate
            * $this->riskFactor($policy) * $this->termFactor($policy->termYears);
        $monthlyPremium = round($basePremium / 12, (int) $this->roundingPrecision) + self::ADMIN_FEE;
        return $monthlyPremium;
    }

    public function annualPremium(Policy $policy): float
    {
        $annualPremium = $this->monthlyPremium($policy) * 12;
        switch ($policy->payFrequency) {
            case 'A':
                $discount = round($annualPremium * self::ANNUAL_DISC_RATE, 2);
                $annualPremium -= $discount;
                break;
            case 'M':
            case 'Q':
                $discount = 0;
                break;
        }
        return $annualPremium;
    }
}
