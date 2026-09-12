# SPDX-License-Identifier: Apache-2.0
# Adapted from megagonlabs/ditto/ditto_light/ditto.py, DittoModel (lines 20-59).
# Upstream: 52985564a93fb11308439516d3e17a033d43ec8f; see DITTO_LICENSE.md.
# Changes: injected local encoder, correct padding attention masks, no device
# global, and a separately padded augmented batch. CLS + Beta MixDA + FC remain.
import numpy as np
from torch import nn


class DittoModel(nn.Module):
    def __init__(self, encoder, alpha_aug=0.8):
        super().__init__()
        self.bert = encoder
        self.alpha_aug = alpha_aug
        self.fc = nn.Linear(encoder.config.hidden_size, 2)

    def forward(self, input_ids, attention_mask, augmented=None):
        original = self.bert(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0, :]
        if augmented is not None:
            changed = self.bert(**augmented).last_hidden_state[:, 0, :]
            coefficient = np.random.beta(self.alpha_aug, self.alpha_aug)
            original = coefficient * original + (1 - coefficient) * changed
        return self.fc(original)
