<script>
    import { createEventDispatcher } from 'svelte';
    const dispatch = createEventDispatcher();

    let title = '';
    let description = '';

    async function submit() {
        const res = await fetch('/api/pins', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ title, description })
        });
        if (res.ok) {
            dispatch('add');
        }
    }

    function cancel() {
        dispatch('cancel');
    }
</script>

<form on:submit|preventDefault={submit}>
    <label>
        Title:
        <input bind:value={title} required>
    </label>
    <label>
        Description:
        <textarea bind:value={description} required></textarea>
    </label>
    <button type="submit">Add</button>
    <button type="button" on:click={cancel}>Cancel</button>
</form>