# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import os
import sys
import tarfile
import threading
import time
import traceback

from OpenBench.models import PGN

from django.db import transaction
from django.core.files.storage import FileSystemStorage

class PGNWatcher(threading.Thread):

    def process_pgn(self, pgn):

        storage = FileSystemStorage()
        tar_path = storage.path(os.path.join('PGNs', '%d.pgn.tar' % (pgn.test_id)))
        pgn_path = storage.path(pgn.filename())

        # Fail before creating an empty archive when the upload is missing.
        if not os.path.isfile(pgn_path):
            raise FileNotFoundError(pgn_path)

        with transaction.atomic():

            # Ensure Media/PGNs exists
            dir_name = os.path.dirname(tar_path)
            os.makedirs(dir_name, exist_ok=True)

            # First PGN will create the initial .tar file
            mode = 'a' if os.path.exists(tar_path) else 'w'
            with tarfile.open(tar_path, mode) as tar:
                tar.add(pgn_path, arcname=pgn.filename())

            # Delete the raw .pgn.bz2 file, and don't process it again
            storage.delete(pgn.filename())
            pgn.processed = True
            pgn.save(update_fields=['processed'])

    def run(self):
        while True:
            for pgn in PGN.objects.filter(processed=False):
                try:
                    self.process_pgn(pgn)
                except:
                    traceback.print_exc()
                    sys.stdout.flush()
            time.sleep(15)
