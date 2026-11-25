"""One read of the commits for many readers.

Every statistic, insight and analytic reads all the commits. With the commits
on disk (see :mod:`commitstore`) each read is a pass over the store - rows
fetched and records built - and those passes were most of the time a large
report took. :func:`fan_out` reads the commits once and hands each chunk to
every reader: a reader is an ordinary function over an iterable of commits,
run in a thread of its own that waits for the next chunk.

The readers see the same commits in the same order as they would reading
the store themselves, so their results are the same. Memory holds a few
chunks at most, however many commits there are.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, TypeVar

T = TypeVar("T")

#: Records handed over at a time, and chunks a reader may have waiting.
CHUNK = 1000
DEPTH = 2

_END = object()


class _Feed:
    """The chunks on their way to one reader."""

    def __init__(self) -> None:
        self.chunks: queue.Queue = queue.Queue(maxsize=DEPTH)
        self.ended = False

    def __iter__(self) -> Iterator[Any]:
        while True:
            chunk = self.chunks.get()
            if chunk is _END:
                self.ended = True
                return
            yield from chunk

    def drain(self) -> None:
        """Take the remaining chunks of a reader that stopped early, so the
        read never waits for it."""
        while not self.ended:
            if self.chunks.get() is _END:
                self.ended = True


def fan_out(records: Iterable[T], readers: Sequence[Callable[[Iterable[T]], Any]]) -> list[Any]:
    """Each reader's result over one read of ``records``, in reader order.

    A reader may stop early; an error in one is raised here once the read is
    over (the first one, in reader order). ``records`` is read once - twice
    or more only if no thread can be started, so it should be a store or a
    list, not a one-off iterator."""
    if len(readers) == 1:
        return [readers[0](records)]
    feeds = [_Feed() for _ in readers]
    results: list[Any] = [None] * len(readers)
    errors: list[BaseException | None] = [None] * len(readers)

    def run(index: int) -> None:
        feed = feeds[index]
        try:
            results[index] = readers[index](iter(feed))
        except BaseException as exc:  # noqa: BLE001 - raised in the caller's thread
            errors[index] = exc
        finally:
            feed.drain()

    threads: list[threading.Thread] = []
    try:
        for index in range(len(readers)):
            thread = threading.Thread(target=run, args=(index,), name=f"reader-{index}", daemon=True)
            thread.start()
            threads.append(thread)
    except RuntimeError:  # no more threads to be had: read once per reader instead
        for feed in feeds[: len(threads)]:
            feed.chunks.put(_END)
        for thread in threads:
            thread.join()
        return [reader(records) for reader in readers]
    try:
        chunk: list[T] = []
        for record in records:
            chunk.append(record)
            if len(chunk) >= CHUNK:
                for feed in feeds:
                    feed.chunks.put(chunk)
                chunk = []
        if chunk:
            for feed in feeds:
                feed.chunks.put(chunk)
    finally:
        # Every reader takes chunks until this, whether it is still reading,
        # stopped early or failed: nothing here waits for ever.
        for feed in feeds:
            feed.chunks.put(_END)
        for thread in threads:
            thread.join()
    for error in errors:
        if error is not None:
            raise error
    return results
