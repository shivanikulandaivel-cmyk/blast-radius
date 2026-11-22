"""Print the selector's most expensive functions from a cProfile dump."""
import pstats, sys
s = pstats.Stats(sys.argv[1])
s.sort_stats("tottime").print_stats(18)
s.sort_stats("cumulative").print_stats(14)
