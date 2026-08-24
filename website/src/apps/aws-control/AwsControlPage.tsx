/**
 * AWS Control — Page 1, Accounts (P0).
 *
 * A thin list-plus-summary surface over the existing profile registry: one card
 * per AWS account with a single health light, a totals strip, and — for a
 * degraded/unknown account — one inline Reconnect action that fetches
 * per-profile guidance. Storage and cost are NOT measured in P0, so their totals
 * render an em dash with a "later phase" hint; a null summary must never show as
 * $0 or 0 GB.
 *
 * The surface is read-only (spec §2.3): every mutation lives in the crew or a
 * dashboard confirmation card, not here. The only writes on this page are the
 * two paid-service consent gates mounted at the bottom, which are their own
 * durable-state components.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Cloud, RefreshCw, Star, ChevronDown, Check, Copy,
} from 'lucide-react'
import { PageHeader, Btn, Badge, StatCard, EmptyState, ContentSkeleton } from '../../components/ui'
import AwsConsentGate from '../../components/AwsConsentGate'
import { i18nT } from '../../i18n/t'
import { awsControlApi, AwsControlError } from './api'
import ConsoleView from './ConsoleView'
import type { AwsAccount, AwsProfile, AccountHealth, ProfileKind, ReconnectPlan } from './types'

/** Tailwind token for each health light, keyed as an `as const` map (literal-safe). */
const HEALTH_DOT: Record<AccountHealth, string> = {
  ok: 'bg-ok',
  degraded: 'bg-warn',
  unknown: 'bg-muted',
}

const HEALTH_LABEL_KEY: Record<AccountHealth, string> = {
  ok: 'apps.awsControl.page.health_ok',
  degraded: 'apps.awsControl.page.health_degraded',
  unknown: 'apps.awsControl.page.health_unknown',
}

const KIND_LABEL_KEY: Record<ProfileKind, string> = {
  sso: 'apps.awsControl.page.kind_sso',
  'credential-process': 'apps.awsControl.page.kind_credential_process',
  other: 'apps.awsControl.page.kind_other',
}

/** One plain sentence of Reconnect guidance per credential kind. */
const RECONNECT_HINT_KEY: Record<ProfileKind, string> = {
  sso: 'apps.awsControl.page.reconnect_hint_sso',
  'credential-process': 'apps.awsControl.page.reconnect_hint_credential_process',
  other: 'apps.awsControl.page.reconnect_hint_other',
}

/** Emphasise the last four digits of an account id, rendered mono ("··· 3792"). */
function AccountId({ account }: { account: string }) {
  if (!account) {
    return (
      <span className="text-muted text-[13px]" data-testid="account-id-unresolved">
        {i18nT('apps.awsControl.page.not_connected_yet')}
      </span>
    )
  }
  const tail = account.slice(-4)
  return (
    <span className="font-mono text-[13px] text-muted" data-testid="account-id">
      ···{' '}
      <span className="text-text-strong font-semibold">{tail}</span>
    </span>
  )
}

function ProfileChip({ profile }: { profile: AwsProfile }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[12px]"
      data-testid="profile-chip"
    >
      <span className="text-text font-medium">{profile.name}</span>
      {profile.default && (
        <Star size={11} className="text-accent fill-accent" aria-label={i18nT('apps.awsControl.page.default_profile')} />
      )}
      <Badge variant="muted">{i18nT(KIND_LABEL_KEY[profile.kind])}</Badge>
      <span className="text-muted font-mono">{profile.region}</span>
    </span>
  )
}

/**
 * Inline Reconnect for a degraded/unknown account. Picks the first unhealthy
 * profile, fetches its reconnect-plan on demand, and shows the command in a mono
 * block with a copy button plus a one-sentence hint for its credential kind.
 */
function ReconnectAction({ account }: { account: AwsAccount }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  // The profile the action repairs: the first that failed its identity probe,
  // falling back to the default/first so the button is never a dead end.
  const failing =
    account.profiles.find((p) => !p.identityOk) ??
    account.profiles.find((p) => p.default) ??
    account.profiles[0]

  const planQ = useQuery<ReconnectPlan>({
    queryKey: ['aws-control', 'reconnect-plan', failing?.name],
    queryFn: () => awsControlApi.reconnectPlan(failing!.name),
    enabled: open && !!failing,
  })

  if (!failing) return null

  const copy = async () => {
    if (!planQ.data) return
    try {
      await navigator.clipboard.writeText(planQ.data.command)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable — the command is still visible to copy by hand */ }
  }

  return (
    <div className="mt-2" data-testid="reconnect">
      <Btn onClick={() => setOpen((v) => !v)} data-testid="reconnect-toggle" aria-expanded={open}>
        <RefreshCw size={13} />
        {i18nT('apps.awsControl.page.reconnect')}
        <ChevronDown size={13} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </Btn>
      {open && (
        <div className="mt-2 rounded-md border border-border bg-bg-elevated p-3 text-[13px]" data-testid="reconnect-panel">
          {planQ.isLoading && (
            <div className="text-muted" data-testid="reconnect-loading">
              {i18nT('apps.awsControl.page.reconnect_loading')}
            </div>
          )}
          {planQ.isError && (
            <div className="text-danger" data-testid="reconnect-error">
              {i18nT('apps.awsControl.page.reconnect_error')}
            </div>
          )}
          {planQ.data && (
            <>
              <p className="text-muted mb-2">{i18nT(RECONNECT_HINT_KEY[planQ.data.kind])}</p>
              <div className="flex items-center gap-2">
                <code
                  className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text"
                  data-testid="reconnect-command"
                >
                  {planQ.data.command}
                </code>
                <Btn onClick={copy} data-testid="reconnect-copy">
                  {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
                  {copied
                    ? i18nT('apps.awsControl.page.copied')
                    : i18nT('apps.awsControl.page.copy')}
                </Btn>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function AccountCard({ account, onOpen }: { account: AwsAccount; onOpen: () => void }) {
  const degraded = account.health !== 'ok'
  return (
    <div
      className="rounded-lg border border-border bg-card px-4 py-3.5 shadow-sm transition-colors hover:border-border-strong"
      data-testid="account-card"
    >
      <div className="flex items-start gap-3">
        <span
          className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${HEALTH_DOT[account.health]}`}
          data-testid="health-dot"
          data-health={account.health}
          role="img"
          aria-label={i18nT(HEALTH_LABEL_KEY[account.health])}
        />
        <div className="min-w-0 flex-1">
          {/* The account identity line is the button that opens the console; the
              Reconnect action below stays a separate control so opening the
              console never triggers a reconnect. */}
          <button
            onClick={onOpen}
            disabled={!account.account}
            className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded text-left cursor-pointer bg-transparent border-none p-0 disabled:cursor-default focus-ring"
            data-testid="account-open"
            aria-label={i18nT('apps.awsControl.page.open_console')}
          >
            <AccountId account={account.account} />
            <span className="text-muted text-[12px]">{i18nT(HEALTH_LABEL_KEY[account.health])}</span>
          </button>
          {account.profiles.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5" data-testid="profile-chips">
              {account.profiles.map((p) => (
                <ProfileChip key={p.name} profile={p} />
              ))}
            </div>
          )}
          {degraded && <ReconnectAction account={account} />}
        </div>
      </div>
    </div>
  )
}

export default function AwsControlPage() {
  // The Console is view state INSIDE this page, not a route: BuiltinAppRoute
  // resolves single-segment routes only. Selecting an account row opens it; the
  // breadcrumb inside ConsoleView clears the selection to return here.
  const [selected, setSelected] = useState<AwsAccount | null>(null)

  const accountsQ = useQuery({
    queryKey: ['aws-control', 'accounts'],
    queryFn: () => awsControlApi.accounts(),
  })

  const refresh = () => accountsQ.refetch()

  if (selected) {
    return <ConsoleView account={selected} onBack={() => setSelected(null)} />
  }

  const header = (
    <PageHeader
      title={i18nT('apps.awsControl.page.title')}
      subtitle={i18nT('apps.awsControl.page.subtitle')}
      actions={
        <Btn onClick={refresh} disabled={accountsQ.isFetching} data-testid="refresh">
          <RefreshCw size={13} className={accountsQ.isFetching ? 'animate-spin' : ''} />
          {i18nT('apps.awsControl.page.refresh')}
        </Btn>
      }
    />
  )

  // A 403 app_disabled means the app was disabled after this bundle loaded (the
  // shell shows its own disabled state on first load). Show the standard
  // disabled-app copy rather than a raw error wall.
  if (accountsQ.isError && accountsQ.error instanceof AwsControlError && accountsQ.error.status === 403) {
    return (
      <div className="flex h-full flex-col">
        {header}
        <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
          <EmptyState
            testId="aws-control-disabled"
            icon={<Cloud />}
            title={i18nT('apps.awsControl.page.disabled_title')}
            subtitle={i18nT('apps.awsControl.page.disabled_body')}
          />
        </div>
      </div>
    )
  }

  const data = accountsQ.data
  const totals = data?.totals

  return (
    <div className="flex h-full flex-col">
      {header}
      <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
        {/* Totals strip. Stored + This month are not measured in P0. */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="totals-strip">
          <StatCard
            label={i18nT('apps.awsControl.page.total_accounts')}
            value={totals ? totals.accounts : undefined}
          />
          <StatCard
            label={i18nT('apps.awsControl.page.connections_healthy')}
            value={
              totals
                ? i18nT('apps.awsControl.page.healthy_fraction', {
                    healthy: totals.profilesHealthy,
                    total: totals.profiles,
                  })
                : undefined
            }
          />
          <StatCard
            label={i18nT('apps.awsControl.page.total_stored')}
            value="—"
            title={i18nT('apps.awsControl.page.measured_later')}
          />
          <StatCard
            label={i18nT('apps.awsControl.page.total_this_month')}
            value="—"
            title={i18nT('apps.awsControl.page.measured_later')}
          />
        </div>

        {accountsQ.isLoading && (
          <div className="mt-6" data-testid="accounts-loading">
            <ContentSkeleton rows={3} />
          </div>
        )}

        {accountsQ.isError && !(accountsQ.error instanceof AwsControlError && accountsQ.error.status === 403) && (
          <div className="mt-6" data-testid="accounts-error">
            <EmptyState
              testId="aws-control-error"
              icon={<Cloud />}
              title={i18nT('apps.awsControl.page.error_title')}
              subtitle={i18nT('apps.awsControl.page.error_body')}
              action={
                <Btn onClick={refresh} data-testid="error-retry">
                  <RefreshCw size={13} />
                  {i18nT('apps.awsControl.page.retry')}
                </Btn>
              }
            />
          </div>
        )}

        {data && data.accounts.length === 0 && (
          <div className="mt-6" data-testid="accounts-empty">
            <EmptyState
              testId="aws-control-empty"
              icon={<Cloud />}
              title={i18nT('apps.awsControl.page.empty_title')}
              subtitle={i18nT('apps.awsControl.page.empty_body')}
            />
          </div>
        )}

        {data && data.accounts.length > 0 && (
          <div className="mt-6 flex flex-col gap-3" data-testid="accounts-list">
            {data.accounts.map((a, i) => (
              <AccountCard key={a.account || `unresolved-${i}`} account={a} onOpen={() => setSelected(a)} />
            ))}
          </div>
        )}

        {/* Paid services — each requires an explicit owner confirmation before the
            first billable call (spec G1). The gates are their own durable-state
            components; this page only mounts them under one intro sentence. */}
        <section className="mt-8" data-testid="paid-services">
          <h2 className="text-sm font-semibold text-text-strong">
            {i18nT('apps.awsControl.page.paid_services_title')}
          </h2>
          <p className="mt-1 mb-3 text-[13px] text-muted">
            {i18nT('apps.awsControl.page.paid_services_intro')}
          </p>
          <div className="flex flex-col gap-3">
            <AwsConsentGate service="s3" />
            <AwsConsentGate service="ce" />
          </div>
        </section>
      </div>
    </div>
  )
}
