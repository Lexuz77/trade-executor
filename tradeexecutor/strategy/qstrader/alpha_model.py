import abc
from typing import Dict

import pandas as pd

from tradeexecutor.state.state import State
from tradingstrategy.universe import Universe


class AlphaModel(abc.ABC):
    """Generate alpha signals based on the current trading universe and execution state;.

    These signals are used by the PortfolioConstructionModel
    to generate target weights for the portfolio.

    Implementing __call__ produces a dictionary keyed by
    Asset and with a scalar value as the signal.
    """

    @abc.abstractmethod
    def __call__(self,
                 ts: pd.Timestamp,
                 state: State,
                 universe: Universe,
                 debug_details: Dict) -> Dict[int, float]:
        pass