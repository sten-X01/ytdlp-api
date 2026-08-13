"""
pproxy Python 3.12 + uvloop ke saath crash ho jaata hai — pproxy khud
apne andar "import uvloop; uvloop.install()" try karta hai agar uvloop
available ho (jo yahan uvicorn[standard] ki wajah se already installed
hai). uvloop ka apna get_event_loop() strict hai — ek RUNNING loop
maangta hai, sirf "set" kiya hua loop kaafi nahi. Fix: pproxy ko uvloop
dikhne hi mat do (import block kar do), taaki wo plain asyncio par hi
gir jaaye — jismein ye problem nahi hai.
"""
import asyncio
import sys

sys.modules["uvloop"] = None  # noqa: pproxy ka internal uvloop import isse fail ho jaayega, aur wo plain asyncio use karega

asyncio.set_event_loop(asyncio.new_event_loop())

from pproxy.server import main  # noqa: E402

if __name__ == "__main__":
    main()

