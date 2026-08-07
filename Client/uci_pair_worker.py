#!/usr/bin/env python3
"""Persistent, fail-closed pair worker for Alice local acceptance runs."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace
import sys

import uci_pair_runner as runner


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: %s" % key)
        value[key] = item
    return value


def reject_json_constant(value):
    raise ValueError("non-finite JSON number: %s" % value)


def load_json(path):
    with open(path, "r", encoding="utf-8", errors="strict") as source:
        return json.load(
            source,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_json_constant,
        )


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def declared_options(engine):
    names = set()
    for line in engine.uci_lines:
        if line.startswith("option name ") and " type " in line:
            names.add(line[len("option name "):].split(" type ", 1)[0])
    return names


def validate_definition(definition):
    if not isinstance(definition, dict):
        raise ValueError("pair-worker definition must be an object")
    allowed_definition_fields = {
        "schema",
        "engines",
        "max_plies",
        "fixed_budget_seconds",
        "stall_grace_seconds",
    }
    if set(definition).difference(allowed_definition_fields):
        raise ValueError("pair-worker definition contains unknown fields")
    if definition.get("schema") != "alice-pair-worker-definition-v1":
        raise ValueError("unsupported pair-worker definition schema")
    engines = definition.get("engines")
    if not isinstance(engines, list) or len(engines) != 2:
        raise ValueError("the pair worker requires exactly two engine definitions")
    max_plies = definition.get("max_plies", 900)
    if type(max_plies) is not int or max_plies <= 0:
        raise ValueError("max_plies must be a positive integer")
    time_controls = set()
    specs = []
    identities = []
    for index, item in enumerate(engines, 1):
        if not isinstance(item, dict):
            raise ValueError("engine definitions must be objects")
        allowed_engine_fields = {
            "path",
            "binary_sha256",
            "cwd",
            "name",
            "evaluator",
            "network_sha256",
            "network_path",
            "time_control",
            "options",
        }
        if set(item).difference(allowed_engine_fields):
            raise ValueError("engine definition contains unknown fields")
        options = item.get("options")
        if not isinstance(options, dict):
            raise ValueError("engine options must be an object")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in options.items()
        ):
            raise ValueError("engine option names and values must be strings")
        if options.get("Threads") != "1" or options.get("Hash") != "512":
            raise ValueError("each engine requires Threads=1 and Hash=512")
        if "UCI_Variant" in options:
            raise ValueError("a dedicated Alice engine must not receive UCI_Variant")
        binary_value = item.get("path")
        if not isinstance(binary_value, str) or not os.path.isabs(binary_value):
            raise ValueError("each engine requires an absolute executable path")
        binary = os.path.abspath(binary_value)
        if not os.path.isfile(binary):
            raise ValueError("each engine executable must be a regular file")
        expected_binary_sha = item.get("binary_sha256")
        if not isinstance(expected_binary_sha, str) or not SHA256_RE.fullmatch(
            expected_binary_sha
        ):
            raise ValueError("each engine requires a lowercase binary SHA-256")
        if sha256_file(binary) != expected_binary_sha:
            raise ValueError("engine %d binary SHA-256 mismatch" % index)
        evaluator = item.get("evaluator")
        if evaluator not in ("Legacy", "Native", "Zero"):
            raise ValueError("engine evaluator must be Legacy, Native, or Zero")
        network_sha = item.get("network_sha256", "")
        if not isinstance(network_sha, str):
            raise ValueError("network_sha256 must be a string")
        if evaluator in ("Legacy", "Native"):
            if not SHA256_RE.fullmatch(network_sha):
                raise ValueError("network-backed evaluators require a SHA-256")
            network_value = item.get("network_path")
            if not isinstance(network_value, str) or not os.path.isabs(network_value):
                raise ValueError("network-backed evaluators require an absolute path")
            network_path = os.path.abspath(network_value)
            if not os.path.isfile(network_path) or sha256_file(network_path) != network_sha:
                raise ValueError("engine %d network SHA-256 mismatch" % index)
            if evaluator == "Legacy":
                selected_path = options.get("EvalFile")
                if not selected_path or not os.path.samefile(selected_path, network_path):
                    raise ValueError("Legacy EvalFile does not select the pinned network")
                if options.get("Use NNUE", "true").lower() != "true":
                    raise ValueError("Legacy evaluation requires Use NNUE=true")
                if options.get("Alice Evaluation") != "Legacy":
                    raise ValueError("Legacy evaluation must be selected explicitly")
            else:
                selected_path = options.get("Alice Native EvalFile")
                if not selected_path or not os.path.samefile(selected_path, network_path):
                    raise ValueError("Native EvalFile does not select the pinned network")
                if options.get("Alice Native SHA256", "").lower() != network_sha:
                    raise ValueError("Native SHA option does not match the pinned network")
                if options.get("Alice Evaluation") != "Native":
                    raise ValueError("Native evaluation must be selected explicitly")
                if options.get("Use NNUE", "true").lower() != "true":
                    raise ValueError("Native evaluation requires Use NNUE=true")
        elif network_sha:
            raise ValueError("Zero evaluation cannot claim a network SHA-256")
        elif (
            options.get("Use NNUE") != "false"
            or options.get("Alice Evaluation") != "Zero"
        ):
            raise ValueError("Zero evaluation must be selected explicitly")
        time_control = str(item.get("time_control", ""))
        time_controls.add(time_control)
        cwd = item.get("cwd") or os.path.dirname(binary) or "."
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError("each engine requires an existing absolute cwd")
        settings = {
            "cmd": binary,
            "dir": cwd,
            "name": item.get("name") or ("engine-%d" % index),
            "proto": "uci",
            "tc": time_control,
            "timemargin": "0",
            "options": dict(options),
        }
        spec = runner.EngineSpec.from_settings(settings, index, strict=True)
        specs.append(spec)
        identities.append({"evaluator": evaluator, "network_sha256": network_sha})
    if len(time_controls) != 1 or not next(iter(time_controls)):
        raise ValueError("both engines require the same non-empty time control")
    fixed_budget = definition.get("fixed_budget_seconds", 600.0)
    stall_grace = definition.get("stall_grace_seconds", 10.0)
    if (
        type(fixed_budget) not in (int, float)
        or not math.isfinite(float(fixed_budget))
        or float(fixed_budget) <= 0
    ):
        raise ValueError("fixed_budget_seconds must be finite and positive")
    if (
        type(stall_grace) not in (int, float)
        or not math.isfinite(float(stall_grace))
        or float(stall_grace) <= 0
    ):
        raise ValueError("stall_grace_seconds must be finite and positive")
    return SimpleNamespace(
        specs=specs,
        identities=identities,
        max_plies=max_plies,
        fixed_budget_s=float(fixed_budget),
        stall_grace_s=float(stall_grace),
        stall_draw_cp=0,
        resign=None,
        draw=None,
        adj_cp=0,
        adj_plies=0,
        shadow_adjudication=False,
        acceptance_mode=True,
        variant="alice",
    )


def authenticate_engine(engine, identity, fen):
    missing = sorted(set(engine.spec.options).difference(declared_options(engine)))
    if missing:
        raise runner.EngineProtocol(
            "%s does not declare configured option(s): %s"
            % (engine.name, ", ".join(missing))
        )
    engine.send("position fen %s" % fen)
    engine.send("eval")
    evaluator = identity["evaluator"]
    token = "alice_native value" if evaluator == "Native" else "legacy_nnue raw"
    lines = engine.read_until(token, timeout=120)
    joined = "\n".join(lines)
    if evaluator == "Native":
        expected_sha = identity["network_sha256"]
        if ("sha256=" + expected_sha) not in joined or not lines[-1].endswith(expected_sha):
            raise runner.EngineProtocol(
                "%s did not confirm the selected Native SHA-256" % engine.name
            )
    elif evaluator == "Legacy":
        expected_sha = identity["network_sha256"]
        if ("sha256=" + expected_sha) not in joined:
            raise runner.EngineProtocol(
                "%s did not confirm the selected Legacy SHA-256" % engine.name
            )
    elif lines[-1] != "legacy_nnue raw 0 adjusted 0":
        raise runner.EngineProtocol(
            "%s did not confirm deterministic Zero evaluation" % engine.name
        )
    engine.sync()


def validate_request(request, seen_ordinals):
    if not isinstance(request, dict):
        raise ValueError("pair request must be an object")
    if set(request) != {
        "schema",
        "pair_ordinal",
        "opening",
        "evidence_directory",
    }:
        raise ValueError("pair request fields do not match the contract")
    if request.get("schema") != "alice-pair-request-v1":
        raise ValueError("unsupported pair-request schema")
    ordinal = request.get("pair_ordinal")
    if type(ordinal) is not int or ordinal < 0 or ordinal in seen_ordinals:
        raise ValueError("pair ordinal must be new and non-negative")
    opening = request.get("opening")
    if not isinstance(opening, dict):
        raise ValueError("pair request requires opening evidence")
    if set(opening) != {"book_line", "raw_line_sha256", "fen", "fen_sha256"}:
        raise ValueError("opening evidence fields do not match the contract")
    if type(opening.get("book_line")) is not int or opening["book_line"] <= 0:
        raise ValueError("opening book line must be a positive integer")
    if not isinstance(opening.get("raw_line_sha256"), str) or not SHA256_RE.fullmatch(
        opening["raw_line_sha256"]
    ):
        raise ValueError("opening raw-line SHA-256 is malformed")
    fen = opening.get("fen")
    if not isinstance(fen, str) or not fen:
        raise ValueError("opening FEN must be a non-empty string")
    expected_fen_sha = hashlib.sha256(fen.encode("utf-8")).hexdigest()
    if opening.get("fen_sha256") != expected_fen_sha:
        raise ValueError("opening FEN SHA-256 mismatch")
    evidence_directory = request.get("evidence_directory")
    if not isinstance(evidence_directory, str) or not os.path.isabs(evidence_directory):
        raise ValueError("evidence_directory must be an absolute path")
    if os.path.normpath(evidence_directory) != evidence_directory:
        raise ValueError("evidence_directory must be normalized")
    parent = os.path.dirname(evidence_directory)
    if not os.path.isdir(parent):
        raise ValueError("evidence parent does not exist")
    return ordinal, fen, evidence_directory


def failure_outcome(fen, message, classification, code, stage):
    outcome = runner.Outcome("1/2-1/2", message, "abandoned", restart=True)
    outcome.root_fen = fen
    outcome.outcome_class = classification
    outcome.failure_code = code
    outcome.failure_stage = stage
    return outcome


class PersistentPairWorker:
    def __init__(self, config):
        self.config = config
        self.engines = None
        self.seen_ordinals = set()

    def close(self):
        if self.engines:
            for engine in self.engines:
                engine.quit()
            self.engines = None

    def boot(self, fen):
        self.close()
        first = runner.Engine(self.config.specs[0])
        try:
            second = runner.Engine(self.config.specs[1])
            authenticate_engine(first, self.config.identities[0], fen)
            authenticate_engine(second, self.config.identities[1], fen)
        except Exception:
            first.quit()
            if "second" in locals():
                second.quit()
            raise
        self.engines = (first, second)

    def play_pair(self, request):
        ordinal, fen, evidence_directory = validate_request(
            request, self.seen_ordinals
        )
        os.mkdir(evidence_directory)
        pgn_path = os.path.join(evidence_directory, "games.pgn")
        result_path = os.path.join(evidence_directory, "result.jsonl")
        open(pgn_path, "xb").close()
        open(result_path, "xb").close()
        games = {}
        for leg in range(2):
            game_no = leg + 1
            try:
                if self.engines is None:
                    self.boot(fen)
                dev, base = self.engines
                dev.new_game()
                base.new_game()
                dev_is_white = leg == 0
                white, black = (dev, base) if dev_is_white else (base, dev)
                outcome = runner.play_game(white, black, fen, self.config)
            except runner.EngineProtocol as error:
                outcome = failure_outcome(
                    fen, str(error), "PROTOCOL_ABORT", "engine-protocol", "startup"
                )
            except (runner.EngineDied, runner.EngineStalled, OSError) as error:
                outcome = failure_outcome(
                    fen, str(error), "OPERATIONAL_ABORT", "engine-startup", "startup"
                )
            if outcome.restart:
                self.close()
            white_name = self.config.specs[0].name if leg == 0 else self.config.specs[1].name
            black_name = self.config.specs[1].name if leg == 0 else self.config.specs[0].name
            runner.write_pgn(
                pgn_path,
                game_no,
                white_name,
                black_name,
                fen,
                outcome,
                self.config.specs[0].tc.label,
                "alice",
                durable=True,
            )
            result_lookup = {"0-1": 0, "1/2-1/2": 1, "1-0": 2}
            absolute_score = result_lookup[outcome.result]
            dev_score = absolute_score if leg == 0 else 2 - absolute_score
            games[game_no] = {
                "game_no": game_no,
                "dev_score": dev_score,
                "outcome": outcome,
            }
        runner.write_machine_pair(result_path, ordinal, games)
        with open(result_path, "r", encoding="ascii", errors="strict") as source:
            result = json.loads(
                source.read(),
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_json_constant,
            )
        self.seen_ordinals.add(ordinal)
        return {
            "schema": "alice-pair-worker-response-v1",
            "pair_ordinal": ordinal,
            "result": result,
            "artifacts": {
                "games_pgn_sha256": sha256_file(pgn_path),
                "result_jsonl_sha256": sha256_file(result_path),
            },
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--definition", required=True)
    args = parser.parse_args(argv)
    config = validate_definition(load_json(args.definition))
    worker = PersistentPairWorker(config)
    try:
        for raw_line in sys.stdin.buffer:
            if not raw_line.strip():
                raise ValueError("blank worker requests are forbidden")
            request = json.loads(
                raw_line.decode("utf-8", errors="strict"),
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_json_constant,
            )
            response = worker.play_pair(request)
            sys.stdout.buffer.write(runner._canonical_json_bytes(response))
            sys.stdout.buffer.flush()
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
