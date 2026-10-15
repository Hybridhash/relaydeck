"""Flet 1.0.3 declarative smoke app: original example, no remote calls."""
import asyncio
import flet as ft


@ft.component
def FindingsPanel():
    query, set_query = ft.use_state("")
    state, set_state = ft.use_state(("loading", ()))
    pending = ft.use_ref(None)
    generation = ft.use_ref(0)

    def stop_request():
        generation.current += 1
        task = pending.current
        if task is not None and not task.done():
            task.cancel()
        pending.current = None

    def start_request():
        generation.current += 1
        request_generation = generation.current
        set_state(("loading", ()))

        async def load():
            try:
                # Replace with a cancellable async API adapter.
                await asyncio.sleep(0.02)
                items = tuple(
                    title for title in ("Exposed service", "Outdated dependency")
                    if query.casefold() in title.casefold()
                )
                if generation.current == request_generation:
                    set_state(("ready", items))
            except asyncio.CancelledError:
                raise
            except Exception:
                if generation.current == request_generation:
                    set_state(("error", ()))

        pending.current = asyncio.create_task(load())

    ft.use_effect(start_request, dependencies=[query], cleanup=stop_request)

    status, items = state
    return ft.Column(
        controls=[
            ft.TextField(
                key="query", label="Search findings", value=query,
                on_change=lambda e: set_query(e.control.value or ""),
            ),
            ft.ProgressRing(key="loading", visible=status == "loading"),
            ft.Text("Could not load findings" if status == "error" else "", key="error"),
            *[ft.Text(title, key=title) for title in items],
        ]
    )


@ft.component
def App():
    count, set_count = ft.use_state(0)
    return ft.SafeArea(
        content=ft.Column(
            controls=[
                ft.Text(str(count), key="count"),
                ft.Button("Increment", key="increment", on_click=lambda: set_count(lambda n: n + 1)),
                FindingsPanel(),
            ],
        )
    )


def main(page: ft.Page):
    page.title = "Flet 1.0.3 declarative smoke"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed="#067A78", use_material3=True)
    page.render(App)


if __name__ == "__main__":
    ft.run(main)
