from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from .stat import stat


class stat_identity(stat):
    DEFAULT_PARAMS = {'geom': 'point', 'position': 'identity'}

    @classmethod
    def _calculate_groups(cls, data, scales, **params):
        return data
