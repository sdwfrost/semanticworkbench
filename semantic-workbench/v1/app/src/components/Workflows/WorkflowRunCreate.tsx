// Copyright (c) Microsoft. All rights reserved.

import {
    Button,
    Dialog,
    DialogActions,
    DialogBody,
    DialogContent,
    DialogOpenChangeData,
    DialogOpenChangeEvent,
    DialogSurface,
    DialogTitle,
    DialogTrigger,
    Field,
    Input,
} from '@fluentui/react-components';
import React from 'react';
import { WorkflowRun } from '../../models/WorkflowRun';
import { useCreateWorkflowRunMutation, useGetWorkflowRunsQuery } from '../../services/workbench';

interface WorkflowRunCreateProps {
    workflowDefinitionId: string;
    open: boolean;
    onOpenChange?: (open: boolean) => void;
    onCreate?: (workflowRun: WorkflowRun) => void;
}

export const WorkflowRunCreate: React.FC<WorkflowRunCreateProps> = (props) => {
    const { workflowDefinitionId, open, onOpenChange, onCreate } = props;
    const { refetch: refetchWorkflowRuns } = useGetWorkflowRunsQuery(workflowDefinitionId);
    const [createWorkflowRun] = useCreateWorkflowRunMutation();
    const [title, setTitle] = React.useState('');
    const [submitted, setSubmitted] = React.useState(false);

    const handleSave = async () => {
        if (submitted) {
            return;
        }
        setSubmitted(true);
        const workflowRun = await createWorkflowRun({
            title,
            workflowDefinitionId,
        }).unwrap();

        await refetchWorkflowRuns();
        onOpenChange?.(false);
        onCreate?.(workflowRun);
    };

    React.useEffect(() => {
        if (!open) {
            return;
        }

        setTitle('');
        setSubmitted(false);
    }, [open]);

    const handleOpenChange = React.useCallback(
        (_event: DialogOpenChangeEvent, data: DialogOpenChangeData) => {
            onOpenChange?.(data.open);
        },
        [onOpenChange],
    );

    return (
        <Dialog open={open} onOpenChange={handleOpenChange}>
            <DialogSurface>
                <DialogBody>
                    <DialogTitle>New Run of Workflow</DialogTitle>
                    <DialogContent>
                        <form
                            onSubmit={(event) => {
                                event.preventDefault();
                                handleSave();
                            }}
                        >
                            <Field label="Title of Run">
                                <Input
                                    disabled={submitted}
                                    value={title}
                                    onChange={(_event, data) => setTitle(data?.value)}
                                    aria-autocomplete="none"
                                />
                            </Field>
                            <button disabled={submitted} type="submit" hidden />
                        </form>
                    </DialogContent>
                    <DialogActions>
                        <DialogTrigger disableButtonEnhancement>
                            <Button appearance="secondary">Cancel</Button>
                        </DialogTrigger>
                        <DialogTrigger>
                            <Button disabled={!title || submitted} appearance="primary" onClick={handleSave}>
                                {submitted ? 'Saving...' : 'Save'}
                            </Button>
                        </DialogTrigger>
                    </DialogActions>
                </DialogBody>
            </DialogSurface>
        </Dialog>
    );
};