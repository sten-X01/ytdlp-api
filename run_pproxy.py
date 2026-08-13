"""
pproxy Python 3.12 + uvloop ke saath crash ho jaata hai kyunki
asyncio.get_event_loop() ko bina kisi pehle-se-chal-rahe loop ke call
karta hai, aur uvloop ka get_event_loop isme RuntimeError deta hai
(stdlib sirf warning deta, uvloop hard error deta hai). Fix: pproxy
import hone se PEHLE khud ek event loop bana kar set kar do.
"""
import asyncio
import sys

asyncio.set_event_loop(asyncio.new_event_loop())

from pproxy.server import main  # noqa: E402

if __name__ == "__main__":
    main()
