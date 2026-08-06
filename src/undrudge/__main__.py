"""Enable ``python -m undrudge``.

`browse` re-invokes undrudge this way for its fzf key bindings, so the picker
always calls back into the interpreter it is already running under instead of
whatever `undrudge` happens to be first on PATH.
"""

from .cli import main

raise SystemExit(main())
