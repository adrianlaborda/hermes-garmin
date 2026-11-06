// Garmin Connect (unofficial) for Hermes Desktop: a read-only readiness panel.
//
// Plain ESM, loaded uncompiled by the Desktop plugin loader: no JSX, no build step, and only the
// three specifiers the loader resolves. Data comes from this package's own backend
// (dashboard/plugin_api.py, mounted at /api/plugins/garmin-connect/) through ctx.rest; the panel
// never sees the Garmin key or token. Colours are the Desktop theme variables only.
import {
  PALETTE_AREA,
  PANES_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  host,
  queryClient,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'garmin-connect'
const ROUTE = '/garmin'
const DAYS = 28
const KEY_PAGE = 'https://connect.garmin.com/app/settings/personalApiToken'
const KEY_ENV = 'GARMIN_PERSONAL_API_KEY'
// The manifest's config_schema `api_key` (type: secret) renders as a masked field on this plugin's own page
// in Settings › Plugins; Desktop saves it to the profile's .env. The page is addressed by the agent key.
const SETTINGS_PATH_TEXT = 'Settings › Plugins › garmin-connect'
const KEY_FIELD = 'Garmin Personal API key'
export const BRIEF_PROMPT =
  'Give me my readiness brief from my Garmin data: call garmin_today and garmin_trends (28 days), ' +
  'compare last night\'s sleep, overnight HRV and resting heart rate with my own baseline, mention any ' +
  'trend flags, and suggest a training load for today. Keep it short.'

let pluginCtx = null

// -- data ---------------------------------------------------------------------------------------

class BackendError extends Error {}

async function rest(path) {
  if (!pluginCtx) throw new BackendError('The Garmin plugin is not loaded.')
  try {
    return await pluginCtx.rest(path, { timeoutMs: 45000 })
  } catch (error) {
    throw new BackendError(String(error?.message || error || 'Request failed'))
  }
}

function useOverview() {
  const profile = useValue(host.state.profile)
  const fresh = useRef(false)
  const query = useQuery({
    queryKey: [ID, profile, 'overview', DAYS],
    queryFn: () => {
      // an explicit refresh skips the backend's five-minute response cache
      const path = `/overview?days=${DAYS}${fresh.current ? '&fresh=true' : ''}`
      fresh.current = false
      return rest(path)
    },
    staleTime: 5 * 60 * 1000,
    refetchInterval: 15 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false
  })
  const { refetch } = query
  const refresh = useCallback(() => {
    fresh.current = true
    return refetch()
  }, [refetch])
  return { ...query, refresh }
}

// -- formatting ---------------------------------------------------------------------------------

// The panel's copy is English, so dates and numbers are formatted in English too.
const locale = () => 'en'

export function parseLocal(text) {
  // Garmin dates are local calendar values ("2026-11-06" or "2026-11-06T07:12"); never shift zones.
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(String(text || ''))
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 12), +(m[5] || 0)) : null
}

export function longDate(text) {
  const d = parseLocal(text)
  return d ? d.toLocaleDateString(locale(), { weekday: 'long', day: 'numeric', month: 'long' }) : ''
}

export function whenText(text, now = new Date()) {
  const d = parseLocal(text)
  if (!d) return ''
  const day = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const diff = Math.round((today - day) / 86400000)
  const time = /T\d/.test(text) ? d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' }) : ''
  const prefix =
    diff === 0
      ? 'Today'
      : diff === 1
        ? 'Yesterday'
        : diff < 7
          ? d.toLocaleDateString(locale(), { weekday: 'long' })
          : d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' })
  return time ? `${prefix}, ${time}` : prefix
}

export function clockText(ms) {
  return ms ? new Date(ms).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' }) : ''
}

export function durationText(hms) {
  const parts = String(hms || '').split(':').map(Number)
  if (parts.length !== 3 || parts.some(Number.isNaN)) return hms || ''
  const [h, m, s] = parts
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m}:${String(s).padStart(2, '0')} min`
}

const num = (value, digits = 0) =>
  typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString(locale(), { maximumFractionDigits: digits, minimumFractionDigits: digits })
    : ''

// -- sparkline ----------------------------------------------------------------------------------

export function sparkGeometry(series, baseline, width = 100, height = 32, pad = 3) {
  const points = (series || []).filter(p => typeof p[1] === 'number' && Number.isFinite(p[1]))
  const values = points.map(p => p[1])
  if (values.length < 2) return null
  // x follows the calendar, so a night without data shows as a longer segment, not a shifted line.
  const days = points.map(p => {
    const d = parseLocal(p[0])
    return d ? Math.round(d.getTime() / 86400000) : NaN
  })
  const byDate = days.every(Number.isFinite) && days[days.length - 1] > days[0]
  const all = typeof baseline === 'number' ? [...values, baseline] : values
  let lo = Math.min(...all)
  let hi = Math.max(...all)
  if (hi - lo < 1e-9) {
    lo -= 1
    hi += 1
  }
  const x = i => (byDate ? (days[i] - days[0]) / (days[days.length - 1] - days[0]) : i / (values.length - 1)) * width
  const y = v => pad + (1 - (v - lo) / (hi - lo)) * (height - 2 * pad)
  const pts = values.map((v, i) => [+x(i).toFixed(2), +y(v).toFixed(2)])
  const line = pts.map(([px, py], i) => `${i ? 'L' : 'M'}${px} ${py}`).join(' ')
  return {
    line,
    area: `${line} L${width} ${height} L0 ${height} Z`,
    last: pts[pts.length - 1],
    baselineY: typeof baseline === 'number' ? +y(baseline).toFixed(2) : null,
    min: Math.min(...values),
    max: Math.max(...values),
    count: values.length
  }
}

function Sparkline({ card }) {
  const geo = sparkGeometry(card.series, card.baseline)
  if (!geo) {
    return jsx('div', { className: 'gc-spark gc-spark-empty', children: 'Not enough history yet' })
  }
  const unit = card.unit ? ` ${card.unit}` : ''
  const label =
    `${card.label}, last ${geo.count} days: from ${num(geo.min, 1)} to ${num(geo.max, 1)}${unit}` +
    (card.baseline != null ? `, baseline ${num(card.baseline, 1)}${unit}` : '')
  return jsxs('svg', {
    className: 'gc-spark',
    viewBox: '0 0 100 32',
    preserveAspectRatio: 'none',
    role: 'img',
    'aria-label': label,
    children: [
      jsx('path', { className: 'gc-spark-area', d: geo.area }),
      geo.baselineY != null &&
        jsx('line', { className: 'gc-spark-base', x1: 0, x2: 100, y1: geo.baselineY, y2: geo.baselineY }),
      jsx('path', { className: 'gc-spark-line', d: geo.line }),
      // a zero-length round-capped line draws a dot that survives the non-uniform scaling
      jsx('line', {
        className: 'gc-spark-dot',
        'data-tone': card.tone,
        x1: geo.last[0],
        x2: geo.last[0],
        y1: geo.last[1],
        y2: geo.last[1]
      })
    ]
  })
}

// -- icons (inline, currentColor) ---------------------------------------------------------------

const ICONS = {
  watch:
    'M8 3h8l1 4M8 3 7 7m10 10-1 4H8l-1-4M7 7h10a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Zm5 3v2.5l1.5 1',
  refresh: 'M20 11a8 8 0 0 0-14.9-3.5M4 4v4h4M4 13a8 8 0 0 0 14.9 3.5M20 20v-4h-4',
  spark: 'M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z',
  run: 'M13 4.5a1.5 1.5 0 1 0 3 0 1.5 1.5 0 0 0-3 0M7 21l3-4 2.5 1V13l-3-2.5L7 13m5.5 5L16 21m-6.5-10.5L13 8l3 3h3',
  bike: 'M5 18a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm14 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM12 18v-4l-3-3 4-3 2 3h3M14 5.5a1 1 0 1 0 2 0 1 1 0 0 0-2 0',
  swim: 'M2 18c2 1.3 4 1.3 6 0s4-1.3 6 0 4 1.3 6 0M2 14c2 1.3 4 1.3 6 0s4-1.3 6 0 4 1.3 6 0M8 10l3-3 3 2 3-3M16 4.5a1.5 1.5 0 1 0 3 0 1.5 1.5 0 0 0-3 0',
  pulse: 'M3 12h4l2-5 4 10 2-5h6',
  key: 'M15 7a3 3 0 1 1-2.8 4H4v3h3v-2h2v2h3.2A3 3 0 0 1 15 7Z',
  alert: 'M12 9v4m0 4h.01M10.3 3.9 2.6 17.4A2 2 0 0 0 4.3 20.4h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  external: 'M14 4h6v6m0-6-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  copy: 'M8 8h11v11H8zM5 16V5h11'
}

function Icon({ name, className = 'gc-icon' }) {
  return jsx('svg', {
    className,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': 'true',
    focusable: 'false',
    children: jsx('path', { d: ICONS[name] || ICONS.pulse })
  })
}

function activityIcon(type) {
  const t = String(type || '')
  if (/run|trail|treadmill/.test(t)) return 'run'
  if (/cycl|bik|ride/.test(t)) return 'bike'
  if (/swim/.test(t)) return 'swim'
  return 'pulse'
}

// -- ask hermes ---------------------------------------------------------------------------------

const wait = ms => new Promise(resolve => (pluginCtx ? pluginCtx.setTimeout(resolve, ms) : resolve()))

export async function askHermes(prompt = BRIEF_PROMPT) {
  const composer = host.composer
  try {
    // A chat composer is on screen (the pane next to a chat): send it there.
    if (composer?.submit?.(null, prompt)) {
      host.notify({ kind: 'success', message: 'Asked Hermes for your readiness brief.' })
      return true
    }
    // Full-page view: go back to the chat, then send once its composer is mounted.
    host.navigate?.('/')
    for (const delay of [150, 350, 700]) {
      await wait(delay)
      if (composer?.submit?.(null, prompt)) {
        host.notify({ kind: 'success', message: 'Asked Hermes for your readiness brief.' })
        return true
      }
    }
    if (await composer?.insertText?.('new', prompt, { mode: 'block' })) {
      host.notify({ kind: 'info', message: 'The prompt is in a new chat. Press Enter to send it.' })
      return true
    }
  } catch {
    // fall through to the clipboard
  }
  const copied = await pluginCtx?.os?.writeClipboard?.(prompt)
  host.notify({
    kind: copied ? 'info' : 'warning',
    message: copied
      ? 'Open a chat and paste: the readiness prompt is on your clipboard.'
      : 'Open a chat and ask Hermes: "Give me my readiness brief".'
  })
  return false
}

function AskButton({ compact = false }) {
  const busy = useValue(host.state.busy)
  const [sending, setSending] = useState(false)
  const onClick = async () => {
    setSending(true)
    try {
      await askHermes()
    } finally {
      setSending(false)
    }
  }
  return jsxs('button', {
    type: 'button',
    className: compact ? 'gc-ask gc-ask-compact' : 'gc-ask',
    onClick,
    disabled: sending,
    'aria-busy': sending ? 'true' : 'false',
    title: busy ? 'Hermes is working; the request will follow the current turn.' : 'Sends a prompt to the chat',
    children: [jsx(Icon, { name: 'spark' }), jsx('span', { children: 'Ask Hermes for my readiness brief' })]
  })
}

// -- pieces -------------------------------------------------------------------------------------

function Badge({ tone, title, children }) {
  return jsx('span', { className: 'gc-badge', 'data-tone': tone || 'neutral', title, children })
}

function MetricCard({ card }) {
  const hasValue = card.display != null
  return jsxs('article', {
    className: 'gc-card',
    'aria-label': `${card.label}: ${hasValue ? `${card.display}${card.unit ? ' ' + card.unit : ''}` : 'no data yet'}${card.badge ? `, ${card.badge_label || card.badge}` : ''}`,
    children: [
      jsxs('div', {
        className: 'gc-card-head',
        children: [
          jsx('h3', { className: 'gc-card-label', children: card.label }),
          card.badge && jsx(Badge, { tone: card.tone, title: card.badge_label, children: card.badge })
        ]
      }),
      jsxs('div', {
        className: 'gc-card-value',
        children: [
          jsx('span', { className: 'gc-value', children: hasValue ? card.display : '—' }),
          hasValue && card.unit && jsx('span', { className: 'gc-unit', children: card.unit })
        ]
      }),
      jsx('p', { className: 'gc-card-detail', children: hasValue ? card.detail || ' ' : 'No data yet today' }),
      jsx(Sparkline, { card })
    ]
  })
}

const SIGNALS_SHOWN = 4

function Signals({ signals, days }) {
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? signals : signals.slice(0, SIGNALS_SHOWN)
  const hidden = signals.length - visible.length
  return jsxs('section', {
    className: 'gc-section',
    'aria-labelledby': 'gc-signals-title',
    children: [
      jsxs('h2', {
        id: 'gc-signals-title',
        className: 'gc-section-title',
        children: ['Signals', jsx('span', { className: 'gc-section-note', children: `vs your ${days}-day baseline` })]
      }),
      signals.length
        ? jsx('ul', {
            className: 'gc-signals',
            children: visible.map((s, i) =>
              jsxs(
                'li',
                {
                  className: 'gc-signal',
                  'data-tone': s.tone,
                  children: [
                    jsx('span', { className: 'gc-signal-dot', 'aria-hidden': 'true' }),
                    jsxs('div', {
                      children: [
                        jsx('p', { className: 'gc-signal-head', children: s.headline }),
                        s.detail && jsx('p', { className: 'gc-signal-detail', children: s.detail })
                      ]
                    }),
                    jsx('span', {
                      className: 'gc-sr',
                      children: s.tone === 'good' ? '(favourable)' : s.tone === 'bad' ? '(needs attention)' : ''
                    })
                  ]
                },
                `${s.metric}-${s.kind}-${i}`
              )
            )
          })
        : jsx('p', {
            className: 'gc-quiet',
            children: 'Nothing unusual: your numbers are within your normal range.'
          }),
      (hidden > 0 || expanded) &&
        signals.length > SIGNALS_SHOWN &&
        jsx('button', {
          type: 'button',
          className: 'gc-link gc-more',
          'aria-expanded': expanded ? 'true' : 'false',
          onClick: () => setExpanded(!expanded),
          children: expanded ? 'Show fewer' : `Show ${hidden} more`
        })
    ]
  })
}

function Stat({ label, value }) {
  return value
    ? jsxs('div', {
        className: 'gc-stat',
        children: [jsx('dt', { children: label }), jsx('dd', { children: value })]
      })
    : null
}

function LastActivity({ activity }) {
  if (!activity) {
    return jsxs('section', {
      className: 'gc-section',
      'aria-labelledby': 'gc-activity-title',
      children: [
        jsx('h2', { id: 'gc-activity-title', className: 'gc-section-title', children: 'Last activity' }),
        jsx('p', { className: 'gc-quiet', children: 'No activities recorded yet.' })
      ]
    })
  }
  const pace = activity.pace_min_per_km
    ? `${activity.pace_min_per_km} /km`
    : activity.avg_speed_kmh
      ? `${num(activity.avg_speed_kmh, 1)} km/h`
      : ''
  return jsxs('section', {
    className: 'gc-section',
    'aria-labelledby': 'gc-activity-title',
    children: [
      jsx('h2', { id: 'gc-activity-title', className: 'gc-section-title', children: 'Last activity' }),
      jsxs('div', {
        className: 'gc-activity',
        children: [
          jsxs('div', {
            className: 'gc-activity-head',
            children: [
              jsx('span', { className: 'gc-activity-icon', children: jsx(Icon, { name: activityIcon(activity.type) }) }),
              jsxs('div', {
                className: 'gc-activity-title',
                children: [
                  jsx('p', { className: 'gc-activity-name', children: activity.name || activity.type_label }),
                  jsx('p', {
                    className: 'gc-activity-when',
                    children: [activity.type_label, whenText(activity.start_local)].filter(Boolean).join(' · ')
                  })
                ]
              }),
              activity.personal_record && jsx(Badge, { tone: 'good', children: 'PR' })
            ]
          }),
          jsxs('dl', {
            className: 'gc-stats',
            children: [
              jsx(Stat, { label: 'Distance', value: activity.distance_km ? `${num(activity.distance_km, 2)} km` : '' }),
              jsx(Stat, { label: 'Time', value: durationText(activity.duration) }),
              jsx(Stat, { label: activity.pace_min_per_km ? 'Pace' : 'Speed', value: pace }),
              jsx(Stat, { label: 'Avg HR', value: activity.avg_hr_bpm ? `${activity.avg_hr_bpm} bpm` : '' }),
              jsx(Stat, { label: 'Load', value: activity.training_load ? num(activity.training_load) : '' }),
              jsx(Stat, { label: 'Aerobic TE', value: activity.aerobic_te ? num(activity.aerobic_te, 1) : '' })
            ]
          })
        ]
      })
    ]
  })
}

function Header({ date, updatedAt, syncedAt, fetching, onRefresh }) {
  // The watch's last sync matters more than the panel's fetch time: today's totals stop there.
  const updated = syncedAt ? `watch synced ${whenText(syncedAt).replace(/^Today, /, '').replace(/^Yesterday/, 'yesterday')}` : updatedAt ? `updated ${clockText(updatedAt)}` : ''
  const sub = date
    ? `${longDate(date)}${updated ? ` · ${updated}` : ''}`
    : 'Garmin Connect (unofficial)'
  return jsxs('header', {
    className: 'gc-header',
    children: [
      jsx('span', { className: 'gc-mark', children: jsx(Icon, { name: 'watch' }) }),
      jsxs('div', {
        className: 'gc-title',
        children: [jsx('h1', { children: 'Garmin' }), jsx('p', { children: sub })]
      }),
      onRefresh &&
        jsx('button', {
          type: 'button',
          className: 'gc-icon-button',
          onClick: onRefresh,
          disabled: fetching,
          'aria-label': fetching ? 'Refreshing Garmin data' : 'Refresh Garmin data',
          title: 'Refresh',
          children: jsx(Icon, { name: 'refresh', className: fetching ? 'gc-icon gc-spin' : 'gc-icon' })
        })
    ]
  })
}

function Loading() {
  return jsxs('div', {
    className: 'gc-body',
    'aria-busy': 'true',
    children: [
      jsx('p', { className: 'gc-sr', role: 'status', children: 'Loading your Garmin data' }),
      jsx('div', {
        className: 'gc-cards',
        children: Array.from({ length: 6 }, (_, i) => jsx('div', { className: 'gc-card gc-skeleton' }, i))
      }),
      jsx('div', { className: 'gc-skeleton gc-skeleton-wide' })
    ]
  })
}

function openKeyPage() {
  pluginCtx?.os?.openExternal?.(KEY_PAGE).then(ok => {
    if (!ok) host.notify({ kind: 'info', message: KEY_PAGE })
  })
}

// Opens this plugin's settings page (the schema form with the key field), for the profile the panel shows.
export function keySettingsPath(profile) {
  const params = new URLSearchParams({ tab: 'plugins', agent: ID })
  if (profile) params.set('profile', profile)
  return `/settings?${params}`
}

function openKeySettings(profile) {
  try {
    if (typeof host.navigate === 'function') {
      host.navigate(keySettingsPath(profile))
      return
    }
  } catch {
    // fall through to the written path
  }
  host.notify({ kind: 'info', message: `Open ${SETTINGS_PATH_TEXT} and paste your key in “${KEY_FIELD}”.` })
}

function CopyCommand() {
  const command = `hermes config set ${KEY_ENV} <your key>`
  const [copied, setCopied] = useState(false)
  return jsxs('div', {
    className: 'gc-command',
    children: [
      jsx('code', { children: command }),
      jsx('button', {
        type: 'button',
        className: 'gc-icon-button',
        'aria-label': copied ? 'Command copied' : 'Copy command',
        title: copied ? 'Copied' : 'Copy',
        onClick: async () => setCopied(Boolean(await pluginCtx?.os?.writeClipboard?.(command))),
        children: jsx(Icon, { name: 'copy' })
      })
    ]
  })
}

function TerminalHint() {
  return jsxs('details', {
    className: 'gc-terminal',
    children: [
      jsx('summary', { children: 'Using the terminal?' }),
      jsx('p', {
        className: 'gc-fineprint',
        children: 'Run this instead (start the line with a space to keep the key out of your shell history):'
      }),
      jsx(CopyCommand, {})
    ]
  })
}

function Onboarding({ state, message, onRetry }) {
  const profile = useValue(host.state.profile)
  const rejected = state === 'auth_error'
  return jsxs('div', {
    className: 'gc-body gc-onboarding',
    children: [
      jsxs('div', {
        className: 'gc-callout',
        'data-tone': rejected ? 'bad' : 'neutral',
        role: rejected ? 'alert' : undefined,
        children: [
          jsx(Icon, { name: rejected ? 'alert' : 'key' }),
          jsxs('div', {
            children: [
              jsx('p', {
                className: 'gc-callout-title',
                children: rejected ? 'Garmin rejected your API key' : 'Connect your Garmin account'
              }),
              jsx('p', {
                children: rejected
                  ? 'It has probably expired or been deleted. Create a new one and paste it in Hermes in place of the old one.'
                  : 'The panel reads your own data through Garmin’s Personal API. It needs a personal API key; your password is never used.'
              })
            ]
          })
        ]
      }),
      jsxs('ol', {
        className: 'gc-steps',
        children: [
          jsxs('li', {
            children: [
              jsx('p', { children: 'In Garmin Connect, open Settings › Personal API Key and create a key. Pick how long it lasts (30 to 365 days) and copy it: Garmin shows it once.' }),
              jsxs('button', {
                type: 'button',
                className: 'gc-button',
                onClick: openKeyPage,
                children: [jsx('span', { children: 'Open the key page' }), jsx(Icon, { name: 'external' })]
              })
            ]
          }),
          jsxs('li', {
            children: [
              jsx('p', { children: `Paste it in “${KEY_FIELD}” and select Save settings. Never paste it into a chat.` }),
              jsxs('button', {
                type: 'button',
                className: 'gc-button gc-button-primary',
                onClick: () => openKeySettings(profile),
                children: [jsx(Icon, { name: 'key' }), jsx('span', { children: 'Add your API key' })]
              }),
              jsx('p', { className: 'gc-fineprint gc-path', children: `It is in ${SETTINGS_PATH_TEXT}.` }),
              jsx(TerminalHint, {})
            ]
          }),
          jsxs('li', {
            children: [
              jsx('p', { children: 'Come back here and check again.' }),
              jsx('button', { type: 'button', className: 'gc-button', onClick: onRetry, children: 'Check again' })
            ]
          })
        ]
      }),
      message && jsx('p', { className: 'gc-fineprint', children: message })
    ]
  })
}

function ErrorView({ title, message, hint, onRetry }) {
  return jsxs('div', {
    className: 'gc-body',
    children: [
      jsxs('div', {
        className: 'gc-callout',
        'data-tone': 'bad',
        role: 'alert',
        children: [
          jsx(Icon, { name: 'alert' }),
          jsxs('div', {
            children: [
              jsx('p', { className: 'gc-callout-title', children: title }),
              message && jsx('p', { children: message }),
              hint && jsx('p', { className: 'gc-fineprint', children: hint })
            ]
          })
        ]
      }),
      jsx('button', { type: 'button', className: 'gc-button', onClick: onRetry, children: 'Try again' })
    ]
  })
}

function Footer({ status }) {
  const days = status?.key_days_left
  const soon = typeof days === 'number' && days <= 7
  return jsxs('footer', {
    className: 'gc-footer',
    children: [
      soon &&
        jsxs('p', {
          className: 'gc-expiry',
          role: 'note',
          children: [
            jsx(Icon, { name: 'key' }),
            days < 0
              ? 'Your Garmin API key has expired. '
              : days === 0
                ? 'Your Garmin API key expires today. '
                : `Your Garmin API key expires in ${days} day${days === 1 ? '' : 's'}. `,
            jsx('button', { type: 'button', className: 'gc-link', onClick: openKeyPage, children: 'Create a new one' })
          ]
        }),
      jsx('p', {
        children: 'Unofficial plugin · your data from the Garmin Connect Personal API · not medical advice'
      })
    ]
  })
}

// -- panel --------------------------------------------------------------------------------------

export function GarminPanel() {
  const { data, error, isLoading, isFetching, dataUpdatedAt, refresh, refetch } = useOverview()
  const state = data?.state
  let body
  if (isLoading) body = jsx(Loading, {})
  else if (error)
    body = jsx(ErrorView, {
      title: 'The Garmin backend is not reachable',
      message: 'Turn the plugin’s agent half on: in Capabilities › Plugins, switch on Agent in the Garmin row, then select Try again. If it still fails, quit and reopen Hermes.',
      hint: String(error.message || error),
      onRetry: () => refetch()
    })
  else if (state === 'not_configured' || state === 'auth_error')
    body = jsx(Onboarding, { state, message: state === 'auth_error' ? data.message : '', onRetry: refresh })
  else if (state && state !== 'ok')
    body = jsx(ErrorView, {
      title: state === 'rate_limited' ? 'Garmin is busy' : 'Could not load your Garmin data',
      message: data.message,
      onRetry: refresh
    })
  else if (data)
    body = jsxs('div', {
      className: 'gc-body',
      children: [
        jsx('section', {
          className: 'gc-cards',
          'aria-label': 'Readiness',
          children: (data.cards || []).map(card => jsx(MetricCard, { card }, card.id))
        }),
        jsx(AskButton, {}),
        jsxs('div', {
          className: 'gc-columns',
          children: [
            jsx(Signals, { signals: data.signals || [], days: data.window_days || DAYS }),
            jsx(LastActivity, { activity: data.last_activity })
          ]
        }),
        data.freshness_note &&
          jsx('p', {
            className: 'gc-fineprint',
            role: 'note',
            children: `Your watch last synced ${data.sync_age_hours} h ago, so today’s steps, stress and Body Battery stop there. Open Garmin Connect on your phone to sync, then refresh.`
          }),
        data.unavailable &&
          jsx('p', {
            className: 'gc-fineprint',
            children: `Some data could not be loaded (${Object.keys(data.unavailable).join(', ')}). It will be retried on refresh.`
          }),
        jsx(Footer, { status: data.status })
      ]
    })
  return jsxs('div', {
    className: 'gc-root',
    role: 'region',
    'aria-label': 'Garmin readiness',
    'aria-busy': isFetching ? 'true' : 'false',
    children: [
      jsx('style', { children: CSS }),
      jsx(Header, {
        date: state === 'ok' ? data.date : '',
        updatedAt: state === 'ok' ? dataUpdatedAt : 0,
        syncedAt: state === 'ok' ? data.synced_local : '',
        fetching: isFetching,
        onRefresh: refresh
      }),
      body
    ]
  })
}

// -- styles -------------------------------------------------------------------------------------

const CSS = `
.gc-root{--gc-good:var(--ui-green,var(--ui-accent));--gc-bad:var(--ui-red,currentColor);--gc-warn:var(--ui-yellow,var(--ui-text-secondary));--gc-line:var(--ui-stroke-secondary);--gc-soft:var(--ui-stroke-tertiary,var(--ui-stroke-secondary));
  height:100%;min-height:0;overflow:auto;container-type:inline-size;color:var(--ui-text-primary,var(--foreground));font-size:13px;line-height:1.45}
.gc-root *{box-sizing:border-box}
.gc-root :focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px;border-radius:6px}
.gc-root h1,.gc-root h2,.gc-root h3,.gc-root p{margin:0}
.gc-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.gc-icon{width:16px;height:16px;flex:none}
.gc-spin{animation:gc-spin .9s linear infinite}
@keyframes gc-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.gc-spin,.gc-skeleton{animation:none!important}}

.gc-header{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:10px;padding:14px 16px 12px;background:var(--ui-surface-background,var(--background));border-bottom:1px solid var(--gc-soft)}
.gc-mark{width:30px;height:30px;flex:none;display:flex;align-items:center;justify-content:center;border-radius:9px;color:var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 14%,transparent)}
.gc-mark .gc-icon{width:18px;height:18px}
.gc-title{flex:1;min-width:0}
.gc-title h1{font-size:15px;font-weight:600;letter-spacing:-.01em}
.gc-title p{font-size:12px;color:var(--ui-text-tertiary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gc-icon-button{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;flex:none;border:0;border-radius:7px;background:transparent;color:var(--ui-text-secondary);cursor:pointer}
.gc-icon-button:hover{background:var(--ui-control-hover-background);color:var(--ui-text-primary,var(--foreground))}
.gc-icon-button:disabled{cursor:default;opacity:.7}

.gc-body{display:flex;flex-direction:column;gap:16px;padding:14px 16px 18px}
.gc-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.gc-card{position:relative;display:flex;flex-direction:column;min-width:0;padding:10px 12px 8px;border:1px solid var(--gc-line);border-radius:10px;background:var(--ui-bg-card,transparent)}
.gc-card-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:2px 6px;min-width:0}
.gc-card-label{font-size:11px;font-weight:500;letter-spacing:.02em;color:var(--ui-text-tertiary);white-space:nowrap}
.gc-card-value{display:flex;align-items:baseline;gap:4px;margin-top:4px}
.gc-value{font-size:22px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.15}
.gc-unit{font-size:12px;color:var(--ui-text-tertiary)}
.gc-card-detail{margin-top:1px;font-size:11.5px;color:var(--ui-text-secondary);overflow-wrap:anywhere}
.gc-badge{flex:none;display:inline-flex;align-items:center;padding:1px 6px;border-radius:999px;font-size:10.5px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--gc-tone);background:color-mix(in srgb,var(--gc-tone) 14%,transparent);--gc-tone:var(--ui-text-tertiary)}
.gc-badge[data-tone=good],.gc-signal[data-tone=good],.gc-spark-dot[data-tone=good]{--gc-tone:var(--gc-good)}
.gc-badge[data-tone=bad],.gc-signal[data-tone=bad],.gc-spark-dot[data-tone=bad],.gc-callout[data-tone=bad]{--gc-tone:var(--gc-bad)}
.gc-badge[data-tone=warn],.gc-signal[data-tone=warn]{--gc-tone:var(--gc-warn)}
.gc-spark{display:block;width:100%;height:30px;margin-top:8px;overflow:visible}
.gc-spark-empty{display:flex;align-items:flex-end;font-size:10.5px;color:var(--ui-text-quaternary,var(--ui-text-tertiary))}
.gc-spark-line{fill:none;stroke:var(--ui-accent);stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.gc-spark-area{fill:color-mix(in srgb,var(--ui-accent) 10%,transparent);stroke:none}
.gc-spark-base{stroke:var(--ui-text-quaternary,var(--ui-text-tertiary));stroke-width:1;stroke-dasharray:2 3;vector-effect:non-scaling-stroke}
.gc-spark-dot{stroke:var(--gc-tone,var(--ui-accent));stroke-width:6;stroke-linecap:round;vector-effect:non-scaling-stroke;--gc-tone:var(--ui-accent)}

.gc-ask{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;min-height:36px;padding:8px 14px;border:0;border-radius:9px;font:inherit;font-weight:600;cursor:pointer;color:var(--background);background:var(--ui-accent);transition:filter .15s}
.gc-ask:hover{filter:brightness(1.08)}
.gc-ask:disabled{cursor:progress;opacity:.75}

.gc-columns{display:grid;grid-template-columns:minmax(0,1fr);gap:16px}
.gc-section{display:flex;flex-direction:column;gap:8px;min-width:0}
.gc-section-title{display:flex;align-items:baseline;gap:8px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ui-text-tertiary)}
.gc-section-note{font-weight:400;letter-spacing:0;text-transform:none;color:var(--ui-text-quaternary,var(--ui-text-tertiary))}
.gc-signals{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none}
.gc-signal{display:flex;align-items:flex-start;gap:10px;padding:9px 12px;border-radius:9px;background:color-mix(in srgb,var(--gc-tone) 8%,transparent);border:1px solid color-mix(in srgb,var(--gc-tone) 22%,transparent);--gc-tone:var(--ui-text-tertiary)}
.gc-signal-dot{width:8px;height:8px;margin-top:6px;flex:none;border-radius:50%;background:var(--gc-tone)}
.gc-signal-head{font-weight:500}
.gc-signal-detail{font-size:12px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.gc-quiet{padding:10px 12px;border:1px dashed var(--gc-line);border-radius:9px;color:var(--ui-text-tertiary)}

.gc-activity{display:flex;flex-direction:column;gap:10px;padding:12px;border:1px solid var(--gc-line);border-radius:10px;background:var(--ui-bg-card,transparent)}
.gc-activity-head{display:flex;align-items:center;gap:10px;min-width:0}
.gc-activity-icon{width:32px;height:32px;flex:none;display:flex;align-items:center;justify-content:center;border-radius:50%;color:var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 12%,transparent)}
.gc-activity-icon .gc-icon{width:18px;height:18px}
.gc-activity-title{flex:1;min-width:0}
.gc-activity-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gc-activity-when{font-size:12px;color:var(--ui-text-tertiary)}
.gc-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 10px;margin:0}
.gc-stat{min-width:0}
.gc-stat dt{font-size:10.5px;color:var(--ui-text-tertiary)}
.gc-stat dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}

.gc-footer{display:flex;flex-direction:column;gap:6px;padding-top:12px;border-top:1px dashed var(--gc-soft);font-size:11px;color:var(--ui-text-quaternary,var(--ui-text-tertiary))}
.gc-expiry{display:flex;align-items:center;gap:6px;color:var(--gc-warn);font-size:12px}
.gc-link{border:0;padding:0;background:none;font:inherit;color:var(--ui-accent);cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.gc-more{align-self:flex-start;font-size:12px}
.gc-fineprint{font-size:11.5px;color:var(--ui-text-tertiary);overflow-wrap:anywhere}

.gc-callout{display:flex;gap:10px;padding:12px;border-radius:10px;border:1px solid color-mix(in srgb,var(--gc-tone) 28%,transparent);background:color-mix(in srgb,var(--gc-tone) 8%,transparent);--gc-tone:var(--ui-accent)}
.gc-callout>.gc-icon{margin-top:2px;color:var(--gc-tone)}
.gc-callout p{color:var(--ui-text-secondary)}
.gc-callout .gc-callout-title{font-weight:600;color:var(--ui-text-primary,var(--foreground));margin-bottom:2px}
.gc-steps{display:flex;flex-direction:column;gap:14px;margin:0;padding:0 0 0 20px}
.gc-steps li{padding-left:2px}
.gc-steps li::marker{color:var(--ui-text-tertiary);font-weight:600}
.gc-steps li>p{margin-bottom:6px;color:var(--ui-text-secondary)}
.gc-button{align-self:flex-start;display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border:1px solid var(--gc-line);border-radius:8px;font:inherit;font-weight:500;cursor:pointer;color:var(--ui-text-primary,var(--foreground));background:transparent}
.gc-button:hover{background:var(--ui-control-hover-background)}
.gc-button .gc-icon{width:14px;height:14px}
.gc-button-primary{border-color:transparent;color:var(--background);background:var(--ui-accent)}
.gc-button-primary:hover{background:var(--ui-accent);filter:brightness(1.08)}
.gc-command{display:flex;align-items:center;gap:6px;padding:4px 4px 4px 10px;border:1px solid var(--gc-line);border-radius:8px;background:var(--ui-inline-code-background,transparent)}
.gc-command code{flex:1;min-width:0;font-family:var(--font-mono,ui-monospace,monospace);font-size:11.5px;overflow-wrap:anywhere}
.gc-path{margin-top:6px}
.gc-terminal{margin-top:8px}
.gc-terminal summary{cursor:pointer;width:max-content;font-size:11.5px;color:var(--ui-text-tertiary)}
.gc-terminal summary:hover{color:var(--ui-text-secondary)}
.gc-terminal[open] summary{margin-bottom:6px}
.gc-terminal .gc-fineprint{margin-bottom:6px}

.gc-skeleton{min-height:104px;border-color:transparent;background:var(--ui-bg-quaternary,var(--ui-control-hover-background));animation:gc-pulse 1.4s ease-in-out infinite}
.gc-skeleton-wide{min-height:36px;border-radius:9px}
@keyframes gc-pulse{50%{opacity:.55}}

@container (min-width:560px){
  .gc-cards{grid-template-columns:repeat(3,minmax(0,1fr))}
  .gc-body{padding:18px 22px 22px;gap:18px}
  .gc-header{padding:16px 22px 14px}
  .gc-ask{width:auto;align-self:flex-start}
}
@container (min-width:900px){
  .gc-cards{grid-template-columns:repeat(6,minmax(0,1fr))}
  .gc-columns{grid-template-columns:minmax(0,1.2fr) minmax(0,1fr)}
  .gc-body{max-width:1280px}
}
`

// -- registration -------------------------------------------------------------------------------

export default {
  id: ID,
  name: 'Garmin',
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Garmin',
        data: { placement: 'right', width: '380px' },
        render: () => jsx(GarminPanel, {})
      },
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(GarminPanel, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Garmin', codicon: 'pulse' } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'garmin-connect.open',
          label: 'Garmin: Open readiness overview',
          keywords: ['garmin', 'sleep', 'hrv', 'body battery', 'readiness', 'health'],
          run: () => host.navigate(ROUTE)
        }
      },
      {
        id: 'brief',
        area: PALETTE_AREA,
        data: {
          id: 'garmin-connect.brief',
          label: 'Garmin: Ask Hermes for my readiness brief',
          keywords: ['garmin', 'readiness', 'brief', 'recovery', 'training'],
          run: () => void askHermes()
        }
      }
    ])
    // A new profile or backend means another key: drop what the panel cached.
    ctx.onEvent('gateway.ready', () => queryClient.invalidateQueries({ queryKey: [ID] }))
    ctx.onDispose(() => {
      queryClient.removeQueries({ queryKey: [ID] })
      pluginCtx = null
    })
  }
}
