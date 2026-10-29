import { initKeaTests } from '~/test/init'

import { dataCatalogUsageLatestRetrieve, dataCatalogUsageList } from './generated/api'
import type { UsageSnapshotSummaryApi } from './generated/api.schemas'
import { usageLogic } from './usageLogic'

jest.mock('./generated/api', () => ({
    dataCatalogUsageList: jest.fn(),
    dataCatalogUsageLatestRetrieve: jest.fn(),
    dataCatalogUsageRecomputeCreate: jest.fn(),
}))

const scan = (status: UsageSnapshotSummaryApi['status']): UsageSnapshotSummaryApi =>
    ({ id: 'scan', status }) as UsageSnapshotSummaryApi

describe('usageLogic', () => {
    beforeEach(() => {
        jest.useFakeTimers()
        initKeaTests()
        jest.mocked(dataCatalogUsageLatestRetrieve).mockResolvedValue({} as any)
    })

    afterEach(() => {
        jest.useRealTimers()
    })

    it('keeps polling a running scan after a failed history request', async () => {
        jest.mocked(dataCatalogUsageList)
            .mockResolvedValueOnce([scan('computing')])
            .mockRejectedValueOnce(new Error('Bad gateway'))
            .mockResolvedValue([scan('done')])
        const logic = usageLogic()
        logic.mount()

        await jest.advanceTimersByTimeAsync(30_000)
        expect(dataCatalogUsageList).toHaveBeenCalledTimes(2)
        expect(logic.values.scanRunning).toBe(true)

        await jest.advanceTimersByTimeAsync(60_000)
        expect(dataCatalogUsageList).toHaveBeenCalledTimes(3)
        expect(logic.values.scanRunning).toBe(false)
    })
})
