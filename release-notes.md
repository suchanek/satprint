# Release Notes -- v0.4.1

> Released: 2026-10-06

satprint 0.4.1 is a bug-fix release for city models. It came out of slicing the Eiffel Tower and Berlin Mitte presets in Bambu Studio. Short bridges no longer leave decks floating over the water, and lone buildings too small to print are left out. The web app also now loads its new controls on the first visit after an upgrade.

## What changed

**Short bridges stand on a pier.** OpenStreetMap maps gangways out to moored boats as bridges. On the model each one is a deck about a millimeter long that reaches no bank. A deck shorter than the pier spacing got no pier, so it hung above the water with nothing under it, and Bambu Studio warned that the Eiffel Tower model had floating regions. A bridge piece with no bank now always stands on at least one pier in the water, even when piers are turned off. Bridges that reach a bank are unchanged.

**Lone buildings too small to print are left out.** A shed or kiosk under 0.4 mm² on the model is too small for a 0.4 mm nozzle to print with real walls, so it printed as a blob. When it touches no other building it is now dropped. A small building against a neighbor is kept, so city blocks and buildings made of several parts keep their shape. Berlin Mitte loses 114 such buildings. The threshold is `MIN_ALONE_MM2`, also the `min_alone_mm2` parameter of `building_mesh`.

**New controls work on the first load after an upgrade.** The page now requests its script and stylesheet with the version in the URL, so a browser fetches fresh copies after an upgrade. Before, a browser could pair the new page with an old cached script, and controls such as the Roads checkbox showed but did nothing.

## Upgrading

Nothing to do. Models with bridges or very small buildings come out slightly different from 0.4.0. Everything else is unchanged.

---

_Full changelog: [CHANGELOG.md](https://github.com/suchanek/satprint/blob/main/CHANGELOG.md)_
