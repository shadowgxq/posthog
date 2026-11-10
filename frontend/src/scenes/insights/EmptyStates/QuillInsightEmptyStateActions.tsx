import { useActions, useValues } from 'kea'
import { router } from 'kea-router'

import { Button, EmptyContent } from '@posthog/quill'

import { dayjs } from 'lib/dayjs'
import { insightDataLogic } from 'scenes/insights/insightDataLogic'
import { insightLogic } from 'scenes/insights/insightLogic'
import { insightVizDataLogic } from 'scenes/insights/insightVizDataLogic'

import { openInsightEditor } from 'products/product_analytics/frontend/insights/insightScene/openInsightEditor'

const WIDER_RANGE_DAYS = 90

export function QuillInsightEmptyStateActions(): JSX.Element | null {
    const { insight, insightProps, canEditInsight } = useValues(insightLogic)
    const { dateRange, insightData } = useValues(insightVizDataLogic(insightProps))
    const { updateDateRange } = useActions(insightVizDataLogic(insightProps))
    const { query, queryChanged } = useValues(insightDataLogic(insightProps))
    const { location } = useValues(router)

    const resolved = insightData?.resolved_date_range
    const spanDays = resolved ? dayjs(resolved.date_to).diff(dayjs(resolved.date_from), 'day') : null
    const canWiden = dateRange?.date_from !== 'all' && (spanDays === null || spanDays < WIDER_RANGE_DAYS)
    const canEdit = !!insight.short_id && canEditInsight && !location.pathname.endsWith('/edit')

    if (!canWiden && !canEdit) {
        return null
    }

    return (
        <EmptyContent className="flex-row justify-center">
            {canWiden && (
                <Button
                    variant="outline"
                    onClick={() => updateDateRange({ date_from: `-${WIDER_RANGE_DAYS}d`, date_to: null }, true)}
                    data-attr="insight-empty-state-widen-date-range"
                >
                    Show last {WIDER_RANGE_DAYS} days
                </Button>
            )}
            {canEdit && (
                <Button
                    variant="outline"
                    onClick={() =>
                        openInsightEditor({
                            query,
                            insightShortId: insight.short_id,
                            dashboardId: insightProps.dashboardId ?? null,
                            queryChanged,
                            enterEditMode: () => {},
                        })
                    }
                    data-attr="insight-empty-state-edit"
                >
                    Edit
                </Button>
            )}
        </EmptyContent>
    )
}
