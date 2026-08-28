import unittest

from OpenBench.variant_contract import (
    VariantContractError,
    configured_variant_contract,
)


class ThreeCheckVariantContractTests(unittest.TestCase):

    def config(self, contract='LICHESS_THREECHECK_V1'):
        return {
            'engines': {
                '3Check-Stockfish': {'variant_contract': contract},
            },
            'books': {},
        }

    def test_bookless_datagen_uses_the_engine_contract(self):
        self.assertEqual(
            configured_variant_contract(
                self.config(),
                '3Check-Stockfish',
                '3Check-Stockfish',
                'NONE',
            ),
            'LICHESS_THREECHECK_V1',
        )

    def test_threecheck_engine_fails_closed_without_contract(self):
        with self.assertRaisesRegex(
            VariantContractError,
            '3Check workloads require variant_contract=LICHESS_THREECHECK_V1',
        ):
            configured_variant_contract(
                self.config(None),
                '3Check-Stockfish',
                '3Check-Stockfish',
                'NONE',
            )


if __name__ == '__main__':
    unittest.main()
