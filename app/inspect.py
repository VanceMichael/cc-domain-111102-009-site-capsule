from pathlib import Path
from app.wire import Replay
from app.references import inspect_links
print(inspect_links(Path("fixtures/index.html").read_text(),"https://exhibition.invalid/show/index.html"))
