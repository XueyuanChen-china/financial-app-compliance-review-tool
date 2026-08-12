# ruff: noqa: F821
@app.post('/api/loan/disburse')
def disburse():
    return payment_service.transfer()
