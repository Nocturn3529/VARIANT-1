"""Correlated Tools & Keys operations; no alternate configuration or secret store."""
from __future__ import annotations

import service_settings


def register(on):
    @on('service-settings:get', 'service-settings:set', 'service-settings:clear')
    async def command(host, websocket, session, message):
        operation = message['type'].split(':')[1]
        request_id = str(message.get('request_id') or '')
        response = {'type': 'service-settings:result', 'operation': operation,
                    'request_id': request_id, 'ok': False}
        try:
            if not request_id:
                raise service_settings.SettingsValidation('A request ID is required.')
            result = service_settings.snapshot(host) if operation == 'get' else service_settings.mutate(host, operation, message)
            response.update(ok=True, result=result)
        except service_settings.SettingsConflict as exc:
            response['error'] = {'code': 'revision_conflict', 'message': str(exc)}
        except service_settings.SettingsValidation as exc:
            response['error'] = {'code': 'invalid_service_setting', 'message': str(exc)}
        except Exception:
            # Persistence/adapter errors can contain sensitive inputs. Keep
            # their exception text out of the settings response and transcript.
            response['error'] = {'code': 'service_setting_failed', 'message': 'The service setting could not be updated. Refresh and try again.'}
        await websocket.send_json(response)
