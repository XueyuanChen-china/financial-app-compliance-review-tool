export const routes = [{ path: '/login' }, { path: '/loan/apply' }];
export function loadLoan() { return axios.get('/api/loan/detail'); }
