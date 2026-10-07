# ReserveGuard


ReserveGuard is a concurrency-safe restaurant reservation system built to demonstrate reliable booking behavior under real-world conditions such as simultaneous requests, retries, time zones, and reservation updates.


## Live Demo

https://reserve-guard.vercel.app/



\## Features



\- Multiple restaurants with different time zones and booking durations

\- Account signup and login

\- Availability search by date and party size

\- Atomic reservation creation

\- Protection against overlapping bookings

\- Idempotent reservation requests

\- Reservation editing and cancellation

\- Timezone-aware scheduling

\- Responsive React interface

\- SQLite-backed persistence

\- Dockerized backend

\- Automated backend test suite



\## Tech Stack



\### Frontend

\- React

\- TypeScript

\- Vite

\- CSS



\### Backend

\- Python 3.11

\- `ThreadingHTTPServer`

\- SQLite

\- `zoneinfo` / IANA timezone data



\### Infrastructure

\- Docker



\## Project Structure



```text

ReserveGuard/

├── backend/

│   ├── Dockerfile

│   ├── .dockerignore

│   └── service.py

├── frontend/

│   ├── src/

│   ├── package.json

│   ├── vite.config.ts

│   └── index.html

├── tests/

│   └── test\_backend.py

├── .gitignore

└── README.md

