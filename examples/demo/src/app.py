import json


def divide(total, count):
    return total / count


def load(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return divide(data["total"], data["count"])


def main():
    return load("stats.json")


if __name__ == "__main__":
    main()
