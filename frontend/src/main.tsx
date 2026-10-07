import React, { FormEvent, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type RestaurantSummary = {
  id: string;
  name: string;
  timezone: string;
};

type Table = {
  id: string;
  label: string;
  capacity: number;
};

type Restaurant = RestaurantSummary & {
  slot_minutes: number;
  reservation_duration_minutes: number;
  cancellation_cutoff_minutes: number;
  opening_hours: Array<{
    weekday: string;
    opens: string;
    closes: string;
  }>;
  tables: Table[];
};

type Slot = {
  starts_at_local: string;
  starts_at: string;
  available_table_ids: string[];
};

type Reservation = {
  reservation_id: string;
  reference: string;
  restaurant_id: string;
  table_id: string;
  party_size: number;
  status: 'confirmed' | 'cancelled';
  starts_at_local: string;
  starts_at: string;
  ends_at: string;
  created_at: string;
};

type AuthResult = {
  user_id: string;
  display_name: string;
  token: string;
};

type ApiError = {
  error?: {
    code?: string;
    message?: string;
  };
};

function todayLocal() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function displayTime(value: string) {
  const time = value.split('T')[1] || value;
  const [hourText, minute] = time.split(':');
  const hour = Number(hourText);
  const suffix = hour >= 12 ? 'PM' : 'AM';
  const displayHour = hour % 12 || 12;
  return `${displayHour}:${minute} ${suffix}`;
}

function displayDateTime(value: string) {
  const [date, time] = value.split('T');
  return `${date} · ${displayTime(`${date}T${time}`)}`;
}

function errorText(status: number, body: ApiError) {
  const code = body?.error?.code;

  const known: Record<string, string> = {
    unauthenticated: 'Please sign in again.',
    table_unavailable: 'That table was just booked. Choose another table or time.',
    cutoff_passed: 'This reservation is too close to its start time to change or cancel.',
    reservation_cancelled: 'This reservation has already been cancelled.',
    idempotency_key_reuse: 'This retry key was already used for a different request.',
    not_on_slot_grid: 'That time is not a valid booking slot.',
    outside_opening_hours: 'That booking falls outside restaurant opening hours.',
    party_exceeds_capacity: 'The selected table is too small for this party.',
    invalid_local_time: 'That local time does not exist because of a daylight-saving transition.',
    validation_failed: 'Please check the information you entered.',
    not_found: 'The requested resource was not found.',
    email_taken: 'An account with this email already exists.',
  };

  if (code && known[code]) return known[code];
  if (body?.error?.message) return body.error.message;
  return `Request failed (${status}).`;
}

async function api<T>(
  path: string,
  init: RequestInit = {},
  token?: string
): Promise<T> {
  const headers = new Headers(init.headers);

  if (init.body) {
    headers.set('Content-Type', 'application/json');
  }

  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  const response = await fetch(`/api${path}`, {
    ...init,
    headers,
  });

  const raw = await response.text();
  let body: any = null;

  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    throw new Error(errorText(response.status, body || {}));
  }

  return body as T;
}

function App() {
  const [token, setToken] = useState('');
  const [displayName, setDisplayName] = useState('');

  const [authMode, setAuthMode] = useState<'login' | 'signup'>('signup');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');

  const [restaurants, setRestaurants] = useState<RestaurantSummary[]>([]);
  const [restaurant, setRestaurant] = useState<Restaurant | null>(null);
  const [restaurantId, setRestaurantId] = useState('');

  const [date, setDate] = useState(todayLocal());
  const [partySize, setPartySize] = useState(2);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [selectedSlot, setSelectedSlot] = useState<Slot | null>(null);

  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const [editReservation, setEditReservation] = useState<Reservation | null>(null);
  const [editPartySize, setEditPartySize] = useState(2);
  const [cancelTarget, setCancelTarget] = useState<Reservation | null>(null);

  const tablesById = useMemo(() => {
    const map = new Map<string, Table>();
    restaurant?.tables.forEach(table => map.set(table.id, table));
    return map;
  }, [restaurant]);

  const restaurantNames = useMemo(() => {
    const map = new Map<string, string>();
    restaurants.forEach(item => map.set(item.id, item.name));
    return map;
  }, [restaurants]);

  const availableSlots = useMemo(
    () => slots.filter(slot => slot.available_table_ids.length > 0),
    [slots]
  );

  const selectedTables = useMemo(() => {
    if (!selectedSlot) return [];

    return selectedSlot.available_table_ids
      .map(id => tablesById.get(id))
      .filter((table): table is Table => Boolean(table));
  }, [selectedSlot, tablesById]);

  useEffect(() => {
    loadRestaurants();
  }, []);

  async function loadRestaurants() {
    try {
      const result = await api<{ restaurants: RestaurantSummary[] }>('/restaurants');
      setRestaurants(result.restaurants);

      if (result.restaurants.length > 0) {
        await selectRestaurant(result.restaurants[0].id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load restaurants.');
    }
  }

  async function selectRestaurant(id: string) {
    setRestaurantId(id);
    setSlots([]);
    setSelectedSlot(null);
    setError('');
    setNotice('');

    try {
      const result = await api<Restaurant>(
        `/restaurants/${encodeURIComponent(id)}`
      );
      setRestaurant(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load restaurant.');
    }
  }

  async function handleAuth(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    setNotice('');

    try {
      const path = authMode === 'signup' ? '/auth/signup' : '/auth/login';

      const payload =
        authMode === 'signup'
          ? { email, password, display_name: name }
          : { email, password };

      const result = await api<AuthResult>(path, {
        method: 'POST',
        body: JSON.stringify(payload),
      });

      setToken(result.token);
      setDisplayName(result.display_name);
      setPassword('');
      setNotice(`Welcome, ${result.display_name}.`);

      await loadReservations(result.token);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Authentication failed.');
    } finally {
      setBusy(false);
    }
  }

  async function searchAvailability(event?: FormEvent) {
    event?.preventDefault();

    if (!restaurantId) return;

    setBusy(true);
    setError('');
    setNotice('');
    setSlots([]);
    setSelectedSlot(null);

    try {
      const params = new URLSearchParams({
        restaurant_id: restaurantId,
        date,
        party_size: String(partySize),
      });

      const result = await api<{
        restaurant_id: string;
        date: string;
        timezone: string;
        slots: Slot[];
      }>(`/availability?${params.toString()}`);

      setSlots(result.slots);

      const firstAvailable =
        result.slots.find(slot => slot.available_table_ids.length > 0) || null;

      setSelectedSlot(firstAvailable);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load availability.');
    } finally {
      setBusy(false);
    }
  }

  async function reserve(tableId: string, startsAtLocal: string) {
    if (!token || !restaurantId) {
      setError('Sign in before making a reservation.');
      return;
    }

    setBusy(true);
    setError('');
    setNotice('');

    try {
      const result = await api<Reservation>(
        '/reservations',
        {
          method: 'POST',
          headers: {
            'Idempotency-Key': crypto.randomUUID(),
          },
          body: JSON.stringify({
            restaurant_id: restaurantId,
            table_id: tableId,
            starts_at_local: startsAtLocal,
            party_size: partySize,
          }),
        },
        token
      );

      setNotice(`Reservation confirmed. Reference: ${result.reference}`);

      await Promise.all([
        loadReservations(),
        searchAvailability(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reservation failed.');
    } finally {
      setBusy(false);
    }
  }

  async function loadReservations(explicitToken?: string) {
    const activeToken = explicitToken || token;
    if (!activeToken) return;

    try {
      const result = await api<{ reservations: Reservation[] }>(
        '/reservations',
        {},
        activeToken
      );

      setReservations(result.reservations);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load reservations.');
    }
  }

  function openEdit(reservation: Reservation) {
    setEditReservation(reservation);
    setEditPartySize(reservation.party_size);
  }

  async function savePartySize() {
    if (!token || !editReservation) return;

    if (!Number.isInteger(editPartySize) || editPartySize < 1) {
      setError('Party size must be a positive whole number.');
      return;
    }

    setBusy(true);
    setError('');
    setNotice('');

    try {
      await api<Reservation>(
        `/reservations/${encodeURIComponent(editReservation.reference)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            party_size: editPartySize,
          }),
        },
        token
      );

      setNotice(`Reservation ${editReservation.reference} updated.`);
      setEditReservation(null);

      await Promise.all([
        loadReservations(),
        searchAvailability(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to update reservation.');
    } finally {
      setBusy(false);
    }
  }

  async function confirmCancellation() {
    if (!token || !cancelTarget) return;

    setBusy(true);
    setError('');
    setNotice('');

    try {
      await api<Reservation>(
        `/reservations/${encodeURIComponent(cancelTarget.reference)}/cancel`,
        {
          method: 'POST',
          body: '{}',
        },
        token
      );

      setNotice(`Reservation ${cancelTarget.reference} cancelled.`);
      setCancelTarget(null);

      await Promise.all([
        loadReservations(),
        searchAvailability(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to cancel reservation.');
    } finally {
      setBusy(false);
    }
  }

  function signOut() {
    setToken('');
    setDisplayName('');
    setReservations([]);
    setNotice('Signed out.');
    setError('');
    setEditReservation(null);
    setCancelTarget(null);
  }

  return (
    <main>
      <header className="hero">
        <div className="hero-glow" />
        <span className="eyebrow">RESERVEGUARD</span>
        <h1>Reservations without the race conditions.</h1>
        <p>
          Search availability, reserve a table, and manage bookings with
          concurrency-safe reservation handling.
        </p>

        <div className="feature-strip">
          <span>Atomic bookings</span>
          <span>Idempotent retries</span>
          <span>Timezone aware</span>
        </div>
      </header>

      {(notice || error) && (
        <section className={`notice-banner ${error ? 'error-banner' : 'success-banner'}`}>
          {error || notice}
        </section>
      )}

      {!token ? (
        <section className="card account-card reveal">
          <div>
            <span className="eyebrow">ACCOUNT</span>
            <h2>
              {authMode === 'signup' ? 'Create an account' : 'Welcome back'}
            </h2>
            <p>
              Sign in to reserve tables and manage your bookings.
            </p>
          </div>

          <form onSubmit={handleAuth}>
            {authMode === 'signup' && (
              <label>
                Display name
                <input
                  value={name}
                  onChange={e => setName(e.target.value)}
                  required
                />
              </label>
            )}

            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                required
              />
            </label>

            <label>
              Password
              <input
                type="password"
                minLength={8}
                value={password}
                onChange={e => setPassword(e.target.value)}
                required
              />
            </label>

            <button disabled={busy}>
              {busy
                ? 'Working…'
                : authMode === 'signup'
                  ? 'Create account'
                  : 'Sign in'}
            </button>
          </form>

          <button
            className="text-button"
            type="button"
            onClick={() =>
              setAuthMode(authMode === 'signup' ? 'login' : 'signup')
            }
          >
            {authMode === 'signup'
              ? 'Already have an account? Sign in'
              : 'Need an account? Sign up'}
          </button>
        </section>
      ) : (
        <section className="card signed-card reveal">
          <div>
            <span className="eyebrow">SIGNED IN</span>
            <h2>{displayName}</h2>
          </div>

          <button className="secondary" onClick={signOut}>
            Sign out
          </button>
        </section>
      )}

      <div className="columns">
        <section className="card reveal">
          <span className="eyebrow">01 / RESTAURANT</span>
          <h2>Find a table</h2>

          <label>
            Restaurant
            <select
              value={restaurantId}
              onChange={e => selectRestaurant(e.target.value)}
            >
              {restaurants.map(item => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>

          {restaurant && (
            <div className="restaurant-meta">
              <span>{restaurant.timezone}</span>
              <span>{restaurant.reservation_duration_minutes} min booking</span>
              <span>{restaurant.tables.length} tables</span>
            </div>
          )}

          <form className="search-form" onSubmit={searchAvailability}>
            <label>
              Date
              <input
                type="date"
                min={todayLocal()}
                value={date}
                onChange={e => setDate(e.target.value)}
                required
              />
            </label>

            <label>
              Guests
              <input
                type="number"
                min="1"
                value={partySize}
                onChange={e => setPartySize(Number(e.target.value))}
                required
              />
            </label>

            <button disabled={busy || !restaurantId}>
              {busy ? 'Checking…' : 'Search availability'}
            </button>
          </form>

          {availableSlots.length > 0 && (
            <div className="availability">
              <div className="section-heading">
                <div>
                  <span className="eyebrow">AVAILABLE TIMES</span>
                  <h3>Choose a time</h3>
                </div>
                <span className="result-count">
                  {availableSlots.length} slots
                </span>
              </div>

              <div className="time-chips">
                {availableSlots.map(slot => (
                  <button
                    type="button"
                    key={slot.starts_at_local}
                    className={
                      selectedSlot?.starts_at_local === slot.starts_at_local
                        ? 'time-chip active'
                        : 'time-chip'
                    }
                    onClick={() => setSelectedSlot(slot)}
                  >
                    {displayTime(slot.starts_at_local)}
                  </button>
                ))}
              </div>

              {selectedSlot && (
                <div className="selected-slot">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">TABLES</span>
                      <h3>{displayTime(selectedSlot.starts_at_local)}</h3>
                    </div>
                    <span className="result-count">
                      {selectedTables.length} available
                    </span>
                  </div>

                  <div className="table-grid">
                    {selectedTables.map(table => (
                      <article className="table-card" key={table.id}>
                        <div>
                          <strong>{table.label}</strong>
                          <span>Up to {table.capacity} guests</span>
                        </div>

                        <button
                          className="secondary"
                          disabled={busy || !token}
                          onClick={() =>
                            reserve(table.id, selectedSlot.starts_at_local)
                          }
                        >
                          {token ? 'Reserve' : 'Sign in first'}
                        </button>
                      </article>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {slots.length > 0 && availableSlots.length === 0 && (
            <div className="empty-state">
              <strong>No tables available</strong>
              <span>Try another date, restaurant, or party size.</span>
            </div>
          )}
        </section>

        <section className="card reveal">
          <div className="section-heading">
            <div>
              <span className="eyebrow">02 / YOUR BOOKINGS</span>
              <h2>My reservations</h2>
            </div>

            {token && (
              <button
                className="secondary compact-button"
                disabled={busy}
                onClick={() => loadReservations()}
              >
                Refresh
              </button>
            )}
          </div>

          {!token && (
            <div className="empty-state">
              <strong>No account connected</strong>
              <span>Sign in to view and manage reservations.</span>
            </div>
          )}

          {token && reservations.length === 0 && (
            <div className="empty-state">
              <strong>No reservations yet</strong>
              <span>Your confirmed bookings will appear here.</span>
            </div>
          )}

          <div className="reservation-list">
            {reservations.map(reservation => (
              <article
                className={`confirmation ${reservation.status}`}
                key={reservation.reference}
              >
                <div className="reservation-top">
                  <span className={`badge ${reservation.status}`}>
                    {reservation.status}
                  </span>
                  <span className="reference">{reservation.reference}</span>
                </div>

                <h3>
                  {restaurantNames.get(reservation.restaurant_id) ||
                    reservation.restaurant_id}
                </h3>

                <dl>
                  <div>
                    <dt>Start</dt>
                    <dd>{displayDateTime(reservation.starts_at_local)}</dd>
                  </div>

                  <div>
                    <dt>Party</dt>
                    <dd>{reservation.party_size} guests</dd>
                  </div>

                  <div>
                    <dt>Table</dt>
                    <dd>
                      {tablesById.get(reservation.table_id)?.label ||
                        reservation.table_id}
                    </dd>
                  </div>
                </dl>

                {reservation.status === 'confirmed' && (
                  <div className="booking-actions">
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => openEdit(reservation)}
                    >
                      Edit party
                    </button>

                    <button
                      className="danger-button"
                      disabled={busy}
                      onClick={() => setCancelTarget(reservation)}
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        </section>
      </div>

      <footer>
        ReserveGuard · SQLite-backed atomic reservations · Idempotent retries ·
        Timezone-aware scheduling
      </footer>

      {editReservation && (
        <div
          className="modal-backdrop"
          onMouseDown={() => !busy && setEditReservation(null)}
        >
          <section
            className="modal"
            onMouseDown={event => event.stopPropagation()}
          >
            <span className="eyebrow">EDIT RESERVATION</span>
            <h2>Change party size</h2>
            <p>
              Update reservation <strong>{editReservation.reference}</strong>.
            </p>

            <label>
              Guests
              <input
                type="number"
                min="1"
                value={editPartySize}
                onChange={e => setEditPartySize(Number(e.target.value))}
              />
            </label>

            <div className="modal-actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setEditReservation(null)}
              >
                Close
              </button>

              <button disabled={busy} onClick={savePartySize}>
                {busy ? 'Saving…' : 'Save changes'}
              </button>
            </div>
          </section>
        </div>
      )}

      {cancelTarget && (
        <div
          className="modal-backdrop"
          onMouseDown={() => !busy && setCancelTarget(null)}
        >
          <section
            className="modal"
            onMouseDown={event => event.stopPropagation()}
          >
            <span className="eyebrow danger-text">CANCEL RESERVATION</span>
            <h2>Cancel this booking?</h2>
            <p>
              Reservation <strong>{cancelTarget.reference}</strong> will be
              cancelled and the table will become available again.
            </p>

            <div className="modal-actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setCancelTarget(null)}
              >
                Keep booking
              </button>

              <button
                className="danger-solid"
                disabled={busy}
                onClick={confirmCancellation}
              >
                {busy ? 'Cancelling…' : 'Cancel reservation'}
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);