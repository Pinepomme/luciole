"""Sample for live mode: background threads and asyncio tasks working at the same time."""

import asyncio
import threading
import time


def checksum(data):
    return sum(data) % 255


def crunch(job):
    for _ in range(25):
        checksum(range(job * 1000))
        time.sleep(0.05)


def start_workers(jobs):
    workers = [threading.Thread(target=crunch, args=(job,), name=f"worker-{job}") for job in jobs]
    for worker in workers:
        worker.start()
    return workers


async def download(url):
    await asyncio.sleep(0.4)
    return len(url)


async def parse(url):
    size = await download(url)
    return size * 2


async def crawl(urls):
    return await asyncio.gather(*(parse(url) for url in urls))


async def main():
    workers = start_workers([1, 2])
    for _ in range(3):
        await crawl(["a.example", "b.example", "c.example"])
    for worker in workers:
        worker.join()


if __name__ == "__main__":
    asyncio.run(main())
