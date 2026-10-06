"""Stable DPO derivative calculation, independent of any training backend."""

import math

def dpo_derivative(chosen_sum, rejected_sum, reference_margin, beta):
    z = beta * (chosen_sum - rejected_sum - reference_margin)
    # Stable softplus(-z) and d(loss)/d(chosen_sum); rejected coefficient is its negative.
    loss = max(-z, 0.0) + math.log1p(math.exp(-abs(z)))
    probability = (math.exp(-z) / (1 + math.exp(-z))) if z >= 0 else 1 / (1 + math.exp(z))
    return loss, -beta * probability
