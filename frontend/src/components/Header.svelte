<script>
  import TopAppBar, {
    Row,
    Section,
    Title,
  } from '@smui/top-app-bar';
  import IconButton from '@smui/icon-button';
  import { onMount } from 'svelte';
  import { user } from '../stores/user.js';

  let topAppBar;

  onMount(async () => {
    const response = await fetch('http://localhost:8000/rest/profile', {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json'
      }
    });

    if (response.ok) {
      const data = await response.json();
      user.set(data);
    } else {
      console.error('Failed to load user data');
    }
  });
</script>

<div class="header">
  <TopAppBar bind:this={topAppBar} variant="standard">
    <Row>
      <Section>
        <IconButton class="material-icons">menu</IconButton>
        <Title>Urban Lens</Title>
      </Section>

      <Section align="end" toolbar>
        {#if $user && $user.avatar}
          <IconButton><img src={$user.avatar} alt="User avatar" class="circle responsive-img" style="width: 50px; height: 50px;"></IconButton>
        {:else}
          <IconButton class="material-icons">account_circle</IconButton>
        {/if}
        <IconButton class="material-icons">map</IconButton>
      </Section>
    </Row>
  </TopAppBar>
</div>