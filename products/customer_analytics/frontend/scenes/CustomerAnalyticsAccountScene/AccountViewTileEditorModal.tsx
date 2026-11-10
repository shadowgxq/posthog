import { useActions, useValues } from 'kea'

import { LemonButton, LemonInput, LemonLabel, LemonModal } from '@posthog/lemon-ui'

import { CodeEditorResizeable } from 'lib/monaco/CodeEditorResizable'

import { ACCOUNT_SQL_QUERY_PLACEHOLDER, isAccountSqlQueryScoped } from '../../components/Accounts/accountSqlQuery'

import { AccountPropertiesWidgetConfiguration } from './AccountPropertiesWidgetConfiguration'
import { accountViewsLogic } from './accountViewsLogic'

interface AccountViewTileEditorModalProps {
    projectId: number
}

export function AccountViewTileEditorModal({ projectId }: AccountViewTileEditorModalProps): JSX.Element {
    const logic = accountViewsLogic({ projectId })
    const { tileEditor, tileSaving } = useValues(logic)
    const { closeTileEditor, saveTileEditor, setTileEditorName, setTileEditorPropertiesConfig, setTileEditorSqlQuery } =
        useActions(logic)
    const sqlQuery = tileEditor?.sqlQuery
    const saveDisabledReason = tileSaving
        ? 'Saving changes'
        : !tileEditor?.name.trim()
          ? 'Enter a tile name'
          : sqlQuery !== undefined && !sqlQuery.trim()
            ? 'Enter a SQL query'
            : sqlQuery !== undefined && !isAccountSqlQueryScoped(sqlQuery)
              ? `Filter the query by ${ACCOUNT_SQL_QUERY_PLACEHOLDER}`
              : undefined

    return (
        <LemonModal
            isOpen={tileEditor !== null}
            onClose={() => {
                if (!tileSaving) {
                    closeTileEditor()
                }
            }}
            title="Edit tile"
            footer={
                <>
                    <LemonButton
                        type="secondary"
                        onClick={closeTileEditor}
                        disabledReason={tileSaving ? 'Saving changes' : undefined}
                    >
                        Cancel
                    </LemonButton>
                    <LemonButton
                        type="primary"
                        onClick={saveTileEditor}
                        loading={tileSaving}
                        disabledReason={saveDisabledReason}
                        data-attr="account-view-tile-save"
                    >
                        Save changes
                    </LemonButton>
                </>
            }
        >
            <div className="flex flex-col gap-1">
                <LemonLabel htmlFor="account-view-tile-name">Tile name</LemonLabel>
                <LemonInput
                    id="account-view-tile-name"
                    value={tileEditor?.name ?? ''}
                    disabled={tileSaving}
                    onChange={setTileEditorName}
                    onPressEnter={tileSaving || saveDisabledReason ? undefined : saveTileEditor}
                    autoFocus
                    maxLength={400}
                    data-attr="account-view-tile-name"
                />
            </div>
            {tileEditor?.propertiesConfig && tileEditor.accountId ? (
                <div className="mt-4">
                    <AccountPropertiesWidgetConfiguration
                        projectId={projectId}
                        accountId={tileEditor.accountId}
                        config={tileEditor.propertiesConfig}
                        saving={tileSaving}
                        onChange={setTileEditorPropertiesConfig}
                    />
                </div>
            ) : null}
            {sqlQuery !== undefined ? (
                <div className="mt-4 flex flex-col gap-1">
                    <LemonLabel>SQL query</LemonLabel>
                    <p className="m-0 text-xs text-secondary">
                        Use {ACCOUNT_SQL_QUERY_PLACEHOLDER} where the account's ID goes. Each account shows only its own
                        rows, for example: <code>where organization_id = {ACCOUNT_SQL_QUERY_PLACEHOLDER}</code>
                    </p>
                    <CodeEditorResizeable
                        language="hogQL"
                        value={sqlQuery}
                        onChange={(value) => setTileEditorSqlQuery(value ?? '')}
                        minHeight="8rem"
                        maxHeight="40vh"
                    />
                </div>
            ) : null}
        </LemonModal>
    )
}
