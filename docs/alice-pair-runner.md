# Alice paired runner

Alice workloads use `Client/uci_pair_runner.py`. The ordinary OpenBench command
surface remains compatible with the client parser, while the local acceptance
surface adds strict create-only evidence and is never treated as an official
OpenBench result.

## Official shadow audits

An Alice shadow audit continues after the first virtual `800/4` win or
`40/8/10` draw threshold and records that point in PGN. Natural Alice terminal
records remain authoritative. The audit must complete 200 color-swapped pairs
at each admitted timing preset with zero shadow inversions.

During a shadow audit, the runner applies the strict Alice terminal protocol.
A missing or contradictory terminal record, malformed move, process failure,
evidence failure, safety-ply limit, or shadow inversion makes the pair
anomalous. The anomalous game is suppressed from the `Finished game` stream,
its class and failure code are preserved in PGN, and the runner exits nonzero
after drain. The client therefore cannot submit that pair as a pentanomial
result, and the audit identifier must be replaced rather than resumed.

## Local acceptance process

`Client/uci_pair_worker.py` is a persistent JSON-lines process for the local
VSTC/STC/LTC controller. It authenticates both binary hashes, network hashes,
declared UCI options, explicit evaluator selection, and evaluator-reported
identity before play. It keeps both engines alive across pairs, restarts after
runtime failures, and writes one create-only PGN and machine result per pair.

The local controller remains the only authority for attempt order, admission,
statistics, and sealing. The process does not calculate LOS and cannot publish
an OpenBench result by itself.
