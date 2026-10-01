import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import mnrh_todo

sys.exit(mnrh_todo.cli(sys.argv[1:]))
