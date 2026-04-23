"""
Fibonacci with memoization example.
"""

from functools import lru_cache


@lru_cache(maxsize=None)
def fibonacci(n: int) -> int:
    """
    Compute the nth Fibonacci number using memoization.
    
    Uses Python's lru_cache decorator to automatically store
    previously computed results, avoiding redundant calculations.
    
    Args:
        n: The position in the Fibonacci sequence (0-indexed).
    
    Returns:
        The nth Fibonacci number.
    
    Raises:
        ValueError: If n is negative.
    
    Examples:
        >>> fibonacci(0)
        0
        >>> fibonacci(10)
        55
    """
    if n < 0:
        raise ValueError("n must be a non-negative integer")
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


# Example usage
if __name__ == "__main__":
    for i in range(15):
        print(f"F({i}) = {fibonacci(i)}")