<script>
    export let pin;
</script>

<script>
    let showInfo = false;

    function toggleInfo() {
        showInfo = !showInfo;
    }
</script>

<div class="pin" style="left: {pin.x}px; top: {pin.y}px;" on:click={toggleInfo}>
    <img src="{pin.icon}" alt="{pin.title}">
    <div class="pin-title">{pin.title}</div>
    {#if showInfo}
        <div class="pin-info">
            <h2>{pin.title}</h2>
            <p>{pin.description}</p>
        </div>
    {/if}
</div>
