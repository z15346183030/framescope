def rate(total, count):
    return total / count


def build(stats):
    return rate(stats["total"], stats["count"])


if __name__ == "__main__":
    build({"total": 10, "count": 0})
