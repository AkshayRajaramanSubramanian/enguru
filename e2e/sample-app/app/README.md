# teamnotes

A small JSON API for private notes. Every request carries a bearer token; each note
belongs to the user who created it and only that user may read, change or delete it.

- `POST /login` returns a token for a username and password.
- `GET /notes?page=1` lists your notes, 20 per page.
- `GET /notes/:id`, `POST /notes`, `PUT /notes/:id`, `DELETE /notes/:id`.
- `POST /notes/:id/tags`, `GET /tags` (tags are per user).

Data lives in a single SQLite file (`teamnotes.db`).
