import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type {
  AwsAccount, DriveStatus, CostReport, LibraryResponse, BackupStatus, SharesResponse,
} from './types'

/* The console reads only through the api client; mocking it keeps every case
 * network-free while leaving `AwsControlError` real for the page's 403/409 paths. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
      iamPolicy: vi.fn(),
      drive: vi.fn(),
      driveBootstrapPreview: vi.fn(),
      driveBootstrapConfirm: vi.fn(),
      driveList: vi.fn(),
      driveDownload: vi.fn(),
      driveUpload: vi.fn(),
      driveDelete: vi.fn(),
      driveShare: vi.fn(),
      shares: vi.fn(),
      shareForget: vi.fn(),
      costs: vi.fn(),
      library: vi.fn(),
      libraryPush: vi.fn(),
      backup: vi.fn(),
      backupRun: vi.fn(),
      backupNightly: vi.fn(),
      backupRestore: vi.fn(),
    },
  }
})

/* The Cost Explorer consent nudge fetches through the shared client. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import ConsoleView from './ConsoleView'
import AwsControlPage from './AwsControlPage'

const ACCOUNT: AwsAccount = {
  account: '111122223333',
  health: 'ok',
  profiles: [
    {
      name: 'personal', region: 'us-west-2', kind: 'sso', identityOk: true,
      account: '111122223333', arn: 'arn:aws:iam::111122223333:role/x', detail: '', default: true,
    },
  ],
  summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
}

const driveExists: DriveStatus = {
  exists: true,
  bucket: 'kirocrew-drive-abc123',
  region: 'us-west-2',
  usage: {
    bytes: 3_500_000_000,
    objects: 42,
    sections: {
      library: { objects: 10, bytes: 1_000_000 },
      drive: { objects: 30, bytes: 3_000_000_000 },
      backup: { objects: 2, bytes: 499_000_000 },
    },
  },
}

const costsFresh: CostReport = {
  fresh: true, monthToDate: 12.5, projected: 30, currency: 'USD',
  byService: [{ service: 'S3', amount: 12.5 }], fetchedAt: '2026-08-24T05:00:00Z',
}

const emptyLibrary: LibraryResponse = { artifacts: [] }
const emptyBackup: BackupStatus = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
const noShares: SharesResponse = { shares: [] }

function stubDrivePresent() {
  vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
  vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
})

describe('AwsControlPage → ConsoleView navigation', () => {
  it('opens the console when an account row is clicked, and the crumb returns', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue({
      accounts: [ACCOUNT],
      totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
      generatedAt: '2026-08-24T05:00:00Z',
    })
    stubDrivePresent()
    renderWithProviders(<AwsControlPage />)

    fireEvent.click(await screen.findByTestId('account-open'))

    // The console mounts (crumb + its own stats strip appear).
    expect(await screen.findByTestId('console-crumb')).toBeTruthy()
    expect(screen.getByTestId('console-stats')).toBeTruthy()

    // The crumb returns to the accounts list.
    fireEvent.click(screen.getByTestId('console-crumb'))
    expect(await screen.findByTestId('accounts-list')).toBeTruthy()
    expect(screen.queryByTestId('console-crumb')).toBeNull()
  })
})

describe('ConsoleView', () => {
  it('renders sites/tasks stats as em-dash ghosts', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    await screen.findByTestId('console-ghosts')
    const stats = screen.getByTestId('console-stats')
    const values = within(stats).getAllByTestId('stat-card-value').map((n) => n.textContent)
    // Sites and Tasks are em dashes; a null figure must never read as 0.
    expect(values.filter((v) => v === '—').length).toBeGreaterThanOrEqual(2)
    expect(stats.textContent).not.toMatch(/\b0\b/)
    // Two dashed app-ghost cards render.
    expect(screen.getAllByTestId('app-ghost')).toHaveLength(2)
  })

  it('shows the drive-missing setup card, previews, then confirms and invalidates', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValueOnce({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    vi.mocked(awsControlApi.driveBootstrapPreview).mockResolvedValue({
      preview: true, account: ACCOUNT.account, region: 'us-west-2', resource: 'kirocrew-drive-abc123',
    })
    vi.mocked(awsControlApi.driveBootstrapConfirm).mockResolvedValue({ created: true, bucket: 'kirocrew-drive-abc123' })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Setup card replaces the drive sections.
    expect(await screen.findByTestId('drive-setup')).toBeTruthy()
    expect(screen.queryByTestId('library-section')).toBeNull()

    // Preview shows the payload, confirm creates the bucket.
    fireEvent.click(screen.getByTestId('drive-preview-btn'))
    expect(await screen.findByTestId('drive-preview')).toHaveTextContent('kirocrew-drive-abc123')

    fireEvent.click(screen.getByTestId('drive-confirm-btn'))
    await waitFor(() => expect(awsControlApi.driveBootstrapConfirm).toHaveBeenCalledWith(ACCOUNT.account))
  })

  it('mints a share link and shows the URL exactly once in the dialog', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockResolvedValue({
      url: 'https://example-presigned/report.pdf?sig=x',
      share: {
        id: 's1', account: ACCOUNT.account, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2026-08-24T06:00:00Z', note: '',
      },
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Share lives in the per-row overflow menu (rows carry at most two
    // sibling controls: Download + More).
    fireEvent.click(await screen.findByTestId('drive-more'))
    fireEvent.click(await screen.findByTestId('drive-share'))
    fireEvent.click(await screen.findByTestId('share-create'))

    const result = await screen.findByTestId('share-result')
    expect(result).toHaveTextContent('https://example-presigned/report.pdf?sig=x')
    // The URL lives only inside the dialog result — not duplicated on the page.
    expect(screen.getAllByText(/example-presigned/).length).toBe(1)
  })

  it('renders the shares ledger with an expires-in countdown', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT.account, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: 'for review',
      }],
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const row = await screen.findByTestId('access-row')
    expect(row).toHaveTextContent('report.pdf')
    expect(row).toHaveTextContent('for review')
    // A relative "expires …" phrase renders (not a raw ISO timestamp).
    expect(row.textContent).not.toContain('2030-01-01')
  })

  it('disables the backup row and spins while a run is in flight', async () => {
    stubDrivePresent()
    // A run that never resolves keeps the row in its busy state.
    vi.mocked(awsControlApi.backupRun).mockReturnValue(new Promise(() => {}) as ReturnType<typeof awsControlApi.backupRun>)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const runBtn = await screen.findByTestId('backup-run-snapshot')
    fireEvent.click(runBtn)
    await waitFor(() => expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(true))
  })

  it('shows the cost-consent nudge when costs report consentMissing', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue({
      fresh: false, monthToDate: 0, projected: 0, currency: 'USD',
      byService: [], fetchedAt: '2026-08-24T05:00:00Z', consentMissing: true,
    })
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    expect(await screen.findByTestId('costs-consent-gate')).toBeTruthy()
    // The consent gate fetches the ce service status.
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('ce'))
  })
})
