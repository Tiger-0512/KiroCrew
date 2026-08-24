import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import type { AwsAccountsResponse, ReconnectPlan } from './types'

/* ── AWS Control api client mock ──────────────────────────────────────────
 * The page reads only through these two methods, so mocking them keeps every
 * case network-free. `AwsControlError` is the real class so `instanceof` and
 * `.status` behave as in production. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
    },
  }
})

/* The two paid-service gates fetch their own consent status through the shared
 * client; stub it so they mount without hitting the network. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi, AwsControlError } from './api'
import { api } from '../../api/client'
import AwsControlPage from './AwsControlPage'

function accountsPayload(overrides: Partial<AwsAccountsResponse> = {}): AwsAccountsResponse {
  return {
    accounts: [
      {
        account: '111122223333',
        health: 'ok',
        profiles: [
          {
            name: 'personal', region: 'us-west-2', kind: 'sso', identityOk: true,
            account: '111122223333', arn: 'arn:aws:iam::111122223333:role/x', detail: '', default: true,
          },
        ],
        summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
      },
      {
        account: '444455556666',
        health: 'degraded',
        profiles: [
          {
            name: 'work', region: 'eu-west-1', kind: 'credential-process', identityOk: false,
            account: '444455556666', arn: '', detail: 'expired', default: true,
          },
        ],
        summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
      },
    ],
    totals: { accounts: 2, profiles: 2, profilesHealthy: 1 },
    generatedAt: '2026-08-24T05:00:00Z',
    ...overrides,
  }
}

function plan(overrides: Partial<ReconnectPlan> = {}): ReconnectPlan {
  return { method: 'terminal', kind: 'credential-process', command: 'aws sso login --profile work', ...overrides }
}

beforeEach(() => {
  vi.clearAllMocks()
  // Keep the consent gates quiet: a never-resolving probe leaves them rendering
  // nothing (the component returns null until its query succeeds), which is fine
  // for the assertions here — we only need the page around them to mount.
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
})

describe('AwsControlPage', () => {
  it('renders one card per account with a health dot carrying its state', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    const cards = await screen.findAllByTestId('account-card')
    expect(cards).toHaveLength(2)

    const dots = screen.getAllByTestId('health-dot')
    expect(dots.map((d) => d.getAttribute('data-health'))).toEqual(['ok', 'degraded'])

    // Account ids render with an emphasized last-4.
    expect(screen.getByText('3333')).toBeTruthy()
    expect(screen.getByText('6666')).toBeTruthy()
    // Profile chip shows name, kind badge, region.
    expect(screen.getByText('personal')).toBeTruthy()
    expect(screen.getByText('us-west-2')).toBeTruthy()
  })

  it('renders null summaries as an em dash, never a zero', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    // Wait for the query to resolve — until then the cards render skeletons.
    await screen.findByText(i18nT('apps.awsControl.page.healthy_fraction', { healthy: 1, total: 2 }))
    const strip = screen.getByTestId('totals-strip')
    const values = within(strip).getAllByTestId('stat-card-value').map((n) => n.textContent)
    // Accounts = 2, healthy = "1 of 2", Stored and This month = em dash.
    expect(values).toContain('—')
    expect(values.filter((v) => v === '—')).toHaveLength(2)
    // A null summary must never surface as 0 / $0 / 0 GB.
    expect(strip.textContent).not.toMatch(/\$?0(\s|$|GB)/)
  })

  it('loads reconnect guidance on the degraded row and shows the command', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    vi.mocked(awsControlApi.reconnectPlan).mockResolvedValue(plan())
    renderWithProviders(<AwsControlPage />)

    fireEvent.click(await screen.findByTestId('reconnect-toggle'))

    await waitFor(() =>
      expect(awsControlApi.reconnectPlan).toHaveBeenCalledWith('work'),
    )
    expect(await screen.findByTestId('reconnect-command')).toHaveTextContent('aws sso login --profile work')
  })

  it('shows a friendly empty state when there are no accounts', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload({ accounts: [], totals: { accounts: 0, profiles: 0, profilesHealthy: 0 } }))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-empty')).toBeTruthy()
    expect(screen.queryByTestId('account-card')).toBeNull()
  })

  it('mounts both paid-service consent gates (s3 and ce)', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    // The gates read their status through the mocked client — one call per service.
    await waitFor(() => {
      expect(api.awsConsent).toHaveBeenCalledWith('s3')
      expect(api.awsConsent).toHaveBeenCalledWith('ce')
    })
    expect(screen.getByTestId('paid-services')).toBeTruthy()
  })

  it('renders the standard disabled-app state on a 403 app_disabled', async () => {
    vi.mocked(awsControlApi.accounts).mockRejectedValue(new AwsControlError('app_disabled', 403))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-disabled')).toBeTruthy()
    expect(screen.queryByTestId('accounts-list')).toBeNull()
    expect(screen.queryByTestId('accounts-error')).toBeNull()
  })

  it('renders an error state with retry on a non-403 failure', async () => {
    vi.mocked(awsControlApi.accounts).mockRejectedValue(new AwsControlError('http_500', 500))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-error')).toBeTruthy()
    expect(screen.getByTestId('error-retry')).toBeTruthy()
  })
})
