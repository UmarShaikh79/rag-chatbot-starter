# Troubleshooting sync

## Notes are not syncing

1. Check that you are signed in to the same account on every device.
2. Make sure the app is updated to the latest version. Versions older than 3.0 can no longer sync.
3. Open Settings > Sync and press Sync now.
4. If you are on the Free plan, check that you have not exceeded the 2-device limit.

## Sync conflicts

When the same note is edited on two devices while offline, Tidepool keeps both versions. The second version is saved as a copy with "(conflict)" in its title so you can merge them by hand.

## Error codes

- **E101**: the session expired. Sign out and sign in again.
- **E204**: the note is larger than the 25 MB limit and cannot be uploaded.
- **E309**: the device clock is wrong. Turn on automatic date and time.

## Resetting the local cache

On desktop, choose Help > Troubleshooting > Reset local cache. This removes the local copy and downloads your notes again. Unsynced changes are lost, so export them first.
