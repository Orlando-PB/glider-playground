"""python -m glider_playground.missions pack <id> [-o out.zip] | import <bundle.zip> | list"""
import argparse

from . import bundle, mission_logic


def main():
    ap = argparse.ArgumentParser(prog="python -m glider_playground.missions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="zip a mission with all its data files")
    p.add_argument("mission"); p.add_argument("-o", "--out")
    i = sub.add_parser("import", help="unpack a bundle and register its files")
    i.add_argument("zip")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "pack":
        print("wrote", bundle.pack(a.mission, a.out))
    elif a.cmd == "import":
        r = bundle.import_zip(a.zip)
        print("imported mission:", r["mission"])
        for f in r["files"]:
            print(f"  {f['name']}: {f['action']}")
    else:
        for m in mission_logic.list_missions():
            print(f"{m['id']:<24} {m['platforms_available']}/{m['platforms']} files  {m['title']}")


if __name__ == "__main__":
    main()
