import argparse
import json
import os
import sys

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def _build_orchestrator() -> Orchestrator:
    fs = FsLayout()
    db_path = os.path.join(fs.base_dir, "core.db")
    conn = init_db(db_path)
    repo = Repository(conn)
    return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=db_path)


def main() -> int:
    parser = argparse.ArgumentParser(prog="parsing-core")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse")
    p_parse.add_argument("file_path")
    p_parse.add_argument("--model", default="stub")
    p_parse.add_argument("--force", action="store_true")

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("task_id")

    p_status = sub.add_parser("status")
    p_status.add_argument("task_id")

    sub.add_parser("list")

    p_purge = sub.add_parser("purge")
    p_purge.add_argument("task_id")

    args = parser.parse_args()
    orch = _build_orchestrator()

    if args.cmd == "parse":
        out = orch.parse_file(args.file_path, force=args.force)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "resume":
        out = orch.resume(args.task_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "status":
        out = orch.status(args.task_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "list":
        out = orch.list_all()
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "purge":
        out = orch.purge(args.task_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
