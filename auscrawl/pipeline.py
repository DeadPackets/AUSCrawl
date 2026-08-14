"""The five crawl phases.

Sections and catalog run on a pool of independent sessions because the search
endpoints are stateful — one bound term per session. Details run on one shared
session at higher parallelism because those endpoints are stateless.
"""

import asyncio
import logging

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from . import config, db, fetch
from .http import RateLimiter, make_client
from .models import Semester
from .parse_json import parse_catalog, parse_sections
from .session import SessionPool

log = logging.getLogger("auscrawl")
console = Console()


def select_terms(all_terms: list[Semester], opts, existing: set[str]) -> list[Semester]:
    terms = sorted(all_terms, key=lambda s: s.term_id, reverse=True)
    if opts.latest:
        return terms[:1]
    if opts.terms:
        wanted = set(opts.terms)
        return [s for s in terms if s.term_id in wanted]
    if opts.resume and not opts.force:
        return [s for s in terms if s.term_id not in existing]
    return terms


def pending_versions(catalog_courses, done: set) -> list[tuple[str, str, str]]:
    """Unique (subject, course_number, term_effective) still needing detail calls."""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for c in catalog_courses:
        key = (c.subject, c.course_number, c.term_effective)
        if not c.term_effective or key in seen or key in done:
            continue
        seen.add(key)
        out.append(key)
    return out


def _progress() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    )


async def run(opts) -> None:
    conn = db.init_db(opts.output, force=opts.force)
    rate = RateLimiter(opts.rate, max_rate=config.MAX_RATE, min_rate=config.MIN_RATE)

    async with make_client(4) as meta_client:
        all_terms = await fetch.fetch_terms(meta_client, rate)
        db.save_semesters(conn, all_terms)
        console.print(f"[green]Terms:[/green] {len(all_terms)} available")

        terms = select_terms(all_terms, opts, db.done_terms(conn))
        console.print(f"[green]Crawling:[/green] {len(terms)} terms")
        if not terms:
            return

        with _progress() as bar:
            task = bar.add_task("Reference", total=len(terms))
            for s in terms:
                db.save_subjects(conn, await fetch.fetch_reference(
                    meta_client, s.term_id, "subject", rate), s.term_id)
                db.save_attributes(conn, await fetch.fetch_reference(
                    meta_client, s.term_id, "attribute", rate), s.term_id)
                bar.advance(task)

    with _progress() as bar:
        task = bar.add_task("Sections", total=len(terms))

        async def one_term(sess, term_id):
            await sess.bind(term_id, "search")
            rows = await fetch.fetch_all_pages(sess, "sections", term_id,
                                               parse_sections)
            db.save_sections(conn, rows)
            bar.advance(task)
            return len(rows)

        async with SessionPool(config.SESSION_POOL_SIZE, rate) as pool:
            counts = await pool.map_terms([s.term_id for s in terms], one_term)
    console.print(f"[green]Sections:[/green] {sum(counts)} rows")
    db.fix_first_seen(conn)

    if opts.no_catalog:
        return

    catalog_courses: list = []
    with _progress() as bar:
        task = bar.add_task("Catalog", total=len(terms))

        async def one_catalog(sess, term_id):
            await sess.bind(term_id, "courseSearch")
            rows = await fetch.fetch_all_pages(sess, "catalog", term_id, parse_catalog)
            db.save_catalog(conn, rows)
            catalog_courses.extend(rows)
            bar.advance(task)
            return len(rows)

        async with SessionPool(config.SESSION_POOL_SIZE, rate) as pool:
            counts = await pool.map_terms([s.term_id for s in terms], one_catalog)
    console.print(f"[green]Catalog:[/green] {sum(counts)} rows")

    if opts.no_details:
        return

    done = set() if opts.force else db.done_course_versions(conn)
    pending = pending_versions(catalog_courses, done)
    console.print(f"[green]Details:[/green] {len(pending)} course versions to fetch")
    if not pending:
        return

    sem = asyncio.Semaphore(config.DETAIL_CONCURRENCY)
    batch: list = []
    async with make_client(config.DETAIL_CONCURRENCY) as client:
        with _progress() as bar:
            task = bar.add_task("Details", total=len(pending))

            async def one_detail(subject, number, term_effective):
                async with sem:
                    d = await fetch.fetch_course_detail(
                        client, term_effective, subject, number, term_effective, rate)
                batch.append(d)
                bar.advance(task)
                if len(batch) >= config.DETAIL_BATCH_SIZE:
                    db.save_course_details(conn, batch[:])
                    batch.clear()

            await asyncio.gather(*(one_detail(*key) for key in pending))

    if batch:
        db.save_course_details(conn, batch)
    console.print("[bold green]Done.[/bold green]")
