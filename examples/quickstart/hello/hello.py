"""Smallest possible service the quickstart pipeline tests and analyses."""


def greet(name: str) -> str:
    if not name:
        raise ValueError("name is required")
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greet("platform"))
