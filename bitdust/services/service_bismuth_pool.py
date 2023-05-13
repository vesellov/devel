#!/usr/bin/python
# service_bismuth_pool.py
#
# Copyright (C) 2008 Veselin Penev, https://bitdust.io
#
# This file (service_bismuth_pool.py) is part of BitDust Software.
#
# BitDust is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# BitDust Software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with BitDust Software.  If not, see <http://www.gnu.org/licenses/>.
#
# Please contact us if you have any questions at bitdust.io@gmail.com
#
#
#
#
"""
..

module:: service_bismuth_pool
"""

from __future__ import absolute_import
from bitdust.services.local_service import LocalService


def create_service():
    return BismuthPoolService()


class BismuthPoolService(LocalService):

    service_name = 'service_bismuth_pool'
    config_path = 'services/bismuth-pool/enabled'

    def dependent_on(self):
        return [
            'service_bismuth_node',
        ]

    def installed(self):
        return True

    def start(self):
        from bitdust.main import settings
        from bitdust.blockchain import bismuth_pool
        bismuth_pool.init(
            data_dir_path=settings.ServiceDir('bismuth_blockchain'),
            node_address='127.0.0.1:5658',
            verbose=True,
        )
        return True

    def stop(self):
        from bitdust.blockchain import bismuth_pool
        bismuth_pool.shutdown()
        return True
